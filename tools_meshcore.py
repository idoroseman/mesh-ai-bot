import asyncio
import inspect
import time
from collections import Counter
from typing import Any, Dict, List

from meshcore import EventType, MeshCore

from config import CONFIG
from tools import CONTACT_TYPES, DEFAULT_MAX_CHANNELS, MAX_LISTED_CONTACTS, MeshTools, format_age

TOOL_TIMEOUT_SECONDS = 30


class MeshCoreTools(MeshTools):
    """Live mesh data the model can look up to answer questions about the network and the bot."""

    background_path = CONFIG["meshcore_background_path"]

    def __init__(self, meshcore: MeshCore, model_name: str, loop: asyncio.AbstractEventLoop | None = None):
        super().__init__(meshcore, model_name)
        self.meshcore = meshcore
        # The event loop that talks to the radio; call() hands the tools over to it
        self.loop = loop
        self.channel_names: Dict[int, str] = {}

    def contact_name(self, public_key: str) -> str:
        contact = self.meshcore.contacts.get(public_key) or {}
        return contact.get("adv_name") or public_key[:12]

    async def get_channel_name(self, idx: int) -> str:
        if idx not in self.channel_names:
            result = await self.meshcore.commands.get_channel(idx)
            if result.type == EventType.ERROR:
                return ""
            self.channel_names[idx] = result.payload.get("channel_name", "")
        return self.channel_names[idx]

    def link_test(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        details = {k: v for k, v in metadata.items() if k != "text"}
        details["received_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        sent = metadata.get("sender_timestamp")
        if isinstance(sent, (int, float)) and sent:
            details["sent_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sent))
        return details

    async def list_contacts(self, query: str = "", kind: str = "") -> Dict[str, Any]:
        await self.meshcore.ensure_contacts(follow=True)
        contacts = list(self.meshcore.contacts.values())
        query = str(query or "").strip().lower()
        kind = str(kind or "").strip().lower()

        def type_name(contact: Dict[str, Any]) -> str:
            return CONTACT_TYPES.get(contact.get("type"), "other")

        matches = [
            c
            for c in contacts
            if (not kind or type_name(c) == kind) and query in c.get("adv_name", "").lower()
        ]
        matches.sort(key=lambda c: c.get("last_advert", 0), reverse=True)

        now = time.time()
        shown = []
        for c in matches[:MAX_LISTED_CONTACTS]:
            hops = c.get("out_path_len", -1)
            entry = {
                "name": c.get("adv_name", ""),
                "type": type_name(c),
                "hops": "unknown" if hops < 0 else hops,
                "heard": format_age(now - c["last_advert"]) + " ago" if c.get("last_advert") else "never",
            }
            if c.get("adv_lat") or c.get("adv_lon"):
                entry["location"] = [round(c["adv_lat"], 4), round(c["adv_lon"], 4)]
            shown.append(entry)
        return {
            "total": len(contacts),
            "by_type": dict(Counter(type_name(c) for c in contacts)),
            "matching": len(matches),
            "contacts": shown,
        }

    async def list_channels(self) -> List[Dict[str, Any]]:
        info = await self.meshcore.commands.send_device_query()
        max_channels = DEFAULT_MAX_CHANNELS if info.type == EventType.ERROR else info.payload.get(
            "max_channels", DEFAULT_MAX_CHANNELS
        )
        channels = []
        for idx in range(max_channels):
            result = await self.meshcore.commands.get_channel(idx)
            if result.type == EventType.ERROR:
                break
            name = result.payload.get("channel_name", "")
            self.channel_names[idx] = name
            if name:
                channels.append({"index": idx, "name": name})
        return channels

    async def bot_health(self) -> Dict[str, Any]:
        info = self.meshcore.self_info
        health: Dict[str, Any] = {
            "bot_uptime": format_age(time.time() - self.started),
            "radio_connected": self.meshcore.is_connected,
            "model": self.model_name,
            "contacts": len(self.meshcore.contacts),
            "counters": dict(self.counters),
            "radio": {
                "name": info.get("name"),
                "freq_mhz": info.get("radio_freq"),
                "bw_khz": info.get("radio_bw"),
                "sf": info.get("radio_sf"),
                "cr": info.get("radio_cr"),
                "tx_power_dbm": info.get("tx_power"),
            },
        }
        commands = self.meshcore.commands
        for key, command in (
            ("core", commands.get_stats_core),
            ("signal", commands.get_stats_radio),
            ("packets", commands.get_stats_packets),
        ):
            result = await command()
            if result is not None and result.type != EventType.ERROR:
                health[key] = result.payload
        device = await commands.send_device_query()
        if device.type != EventType.ERROR:
            health["firmware"] = {k: device.payload.get(k) for k in ("model", "ver", "fw_build")}
        return health

    async def call_async(self, name: str, arguments: Dict[str, Any], metadata: Dict[str, Any]) -> str:
        """Run one tool on the event loop; metadata describes the user message that is being answered."""
        handler = self.find_handler(name, metadata)
        if handler is None:
            return f"Unknown tool: {name}"
        try:
            result = handler(**arguments)
            if inspect.isawaitable(result):
                result = await result
            return self.to_text(result)
        except Exception as exc:
            return f"Tool error: {exc}"

    def call(self, name: str, arguments: Dict[str, Any], metadata: Dict[str, Any]) -> str:
        """Run one tool for the model, which is waited for in a worker thread while the loop stays free."""
        future = asyncio.run_coroutine_threadsafe(self.call_async(name, arguments, metadata), self.loop)
        return future.result(TOOL_TIMEOUT_SECONDS)
