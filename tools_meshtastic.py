import copy
import time
from collections import Counter
from typing import Any, Dict, List

from google.protobuf.json_format import MessageToDict
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import channel_pb2

from config import CONFIG
from tools import TOOL_SPECS, MeshTools, format_age


def meshtastic_specs() -> List[Dict[str, Any]]:
    specs = copy.deepcopy(TOOL_SPECS)
    for spec in specs:
        function = spec["function"]
        function["description"] = DESCRIPTIONS.get(function["name"], function["description"])
        if "kind" in function["parameters"]["properties"]:
            function["parameters"]["properties"]["kind"]["description"] = KIND_DESCRIPTION
    return specs


def node_id_of(packet: Dict[str, Any]) -> str:
    # fromId is missing on the first packet from a node the radio does not know yet
    return f"!{packet.get('from', 0):08x}"


def node_role(info: Dict[str, Any]) -> str:
    # A node with the default role announces no role at all
    return info.get("role", DEFAULT_ROLE).lower()


def node_kind(info: Dict[str, Any]) -> str:
    role = node_role(info)
    return next((kind for kind, roles in KIND_ROLES.items() if any(r in role for r in roles)), "other")


class MeshtasticTools(MeshTools):
    """Live mesh data the model can look up to answer questions about the network and the bot."""

    background_path = CONFIG["meshtastic_background_path"]

    def __init__(self, meshtastic: MeshInterface, model_name: str):
        super().__init__(meshtastic, model_name)

    def node_name(self, node_id: str) -> str:
        return (self.radio.nodes or {}).get(node_id, {}).get("user", {}).get("longName") or node_id

    def channel_name(self, index: int) -> str:
        channels = self.radio.localNode.channels or []
        name = channels[index].settings.name if index < len(channels) else ""
        return name or f"channel {index}"

    def link_test(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """How the user's message reached the bot; metadata is the received packet."""
        details: Dict[str, Any] = {
            "sender": self.node_name(node_id_of(metadata)),
            "channel": metadata.get("channel", 0),
            "received_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        # A hop limit of 0 is left out of the packet; without hopStart the sender's firmware does not say
        if "hopStart" in metadata:
            hops = metadata["hopStart"] - metadata.get("hopLimit", 0)
            details["hops"] = hops
            details["route"] = "heard directly, no relays" if hops == 0 else f"relayed through {hops} node(s)"
        else:
            details["hops"] = "unknown"
        for key, name in (("rxSnr", "snr_db"), ("rxRssi", "rssi_dbm"), ("viaMqtt", "via_mqtt")):
            if key in metadata:
                details[name] = metadata[key]
        return details

    def list_contacts(self, query: str = "", kind: str = "") -> Dict[str, Any]:
        """The nodes the radio knows, most recently heard first."""
        # A node the radio has heard but that has not announced itself yet has no user details
        nodes = [(node_id, node, node.get("user") or {}) for node_id, node in dict(self.radio.nodes or {}).items()]
        query = str(query or "").strip().lower()
        kind = str(kind or "").strip().lower()
        # A kind that is not one of the contact types is taken as a Meshtastic role
        roles = KIND_ROLES.get(kind, (kind,))

        def names(node_id: str, info: Dict[str, Any]) -> str:
            return f"{info.get('longName', '')} {info.get('shortName', '')} {node_id}".lower()

        matches = [
            (node_id, node, info)
            for node_id, node, info in nodes
            if (not kind or any(r in node_role(info) for r in roles)) and query in names(node_id, info)
        ]
        matches.sort(key=lambda match: match[1].get("lastHeard") or 0, reverse=True)

        now = time.time()
        shown = []
        for node_id, node, info in matches[:MAX_LISTED_CONTACTS]:
            entry = {
                "name": info.get("longName") or info.get("shortName") or node_id,
                "short": info.get("shortName", ""),
                "id": node_id,
                "type": node_kind(info),
                "role": node_role(info),
                "hops": node.get("hopsAway", "unknown"),
                "heard": format_age(now - node["lastHeard"]) + " ago" if node.get("lastHeard") else "never",
            }
            if info.get("hwModel"):
                entry["hardware"] = info["hwModel"]
            if info.get("isLicensed"):
                entry["licensed_ham"] = True
            position = node.get("position") or {}
            if position.get("latitude") or position.get("longitude"):
                entry["location"] = [round(position.get("latitude", 0), 4), round(position.get("longitude", 0), 4)]
            shown.append(entry)
        return {
            "total": len(nodes),
            "by_type": dict(Counter(node_kind(info) for _, _, info in nodes)),
            "matching": len(matches),
            "contacts": shown,
        }

    def list_channels(self) -> List[Dict[str, Any]]:
        channels = []
        for channel in self.radio.localNode.channels or []:
            if channel.role == channel_pb2.Channel.Role.DISABLED:
                continue
            # Only the name and the role; the settings also hold the channel key
            role = channel_pb2.Channel.Role.Name(channel.role).lower()
            channels.append({"index": channel.index, "name": channel.settings.name, "role": role})
        return channels

    def bot_health(self) -> Dict[str, Any]:
        node = self.radio.getMyNodeInfo() or {}
        user = node.get("user", {})
        health: Dict[str, Any] = {
            "bot_uptime": format_age(time.time() - self.started),
            "model": self.model_name,
            "contacts": len(self.radio.nodes or {}),
            "counters": dict(self.counters),
            "radio": {
                "name": user.get("longName"),
                "short": user.get("shortName"),
                "id": user.get("id"),
                "hardware": user.get("hwModel"),
                "role": node_role(user),
            },
        }
        # Battery, voltage, channel and air utilization and uptime, as the radio last reported them
        if node.get("deviceMetrics"):
            health["device"] = node["deviceMetrics"]
        lora = MessageToDict(self.radio.localNode.localConfig.lora)
        if lora:
            health["lora"] = lora
        return health
