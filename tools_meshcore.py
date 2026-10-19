import asyncio
import hashlib
import inspect
import json
import time
import urllib.request
from collections import Counter
from typing import Any, Dict, List

from meshcore import EventType, MeshCore

from config import CONFIG
from tools import CONTACT_TYPES, DEFAULT_MAX_CHANNELS, MAX_LISTED_CONTACTS, MeshTools, format_age

TOOL_TIMEOUT_SECONDS = 30

MAP_UPLOAD_URL = "https://map.meshcore.io/api/v1/uploader/node"
MAP_REUPLOAD_SECONDS = 3600
ADVERT_PAYLOAD_TYPE = 4
MAP_NODE_TYPES = {2: "repeater", 3: "room", 4: "sensor"}

# Ed25519 (RFC 8032) with MeshCore's expanded 64-byte private keys, which the standard libraries cannot sign with
_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _add(p, q):
    (x1, y1), (x2, y2) = p, q
    t = _D * x1 * x2 * y1 * y2 % _P
    return (
        (x1 * y2 + x2 * y1) * pow(1 + t, _P - 2, _P) % _P,
        (y1 * y2 + x1 * x2) * pow(1 - t, _P - 2, _P) % _P,
    )


def _mul(k: int, p):
    result = (0, 1)
    while k:
        if k & 1:
            result = _add(result, p)
        p = _add(p, p)
        k >>= 1
    return result


def _x_from_y(y: int, sign: int) -> int:
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _I % _P
    if (x * x - x2) % _P:
        raise ValueError("not a point on the curve")
    return _P - x if x & 1 != sign else x


_B = (_x_from_y(4 * pow(5, _P - 2, _P) % _P, 0), 4 * pow(5, _P - 2, _P) % _P)


def _encode(p) -> bytes:
    return (p[1] | ((p[0] & 1) << 255)).to_bytes(32, "little")


def _decode(data: bytes):
    value = int.from_bytes(data, "little")
    y = value & ((1 << 255) - 1)
    if y >= _P:
        raise ValueError("not a point on the curve")
    return (_x_from_y(y, value >> 255), y)


def _hash(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little")


def ed25519_public_key(expanded_private_key: bytes) -> bytes:
    return _encode(_mul(int.from_bytes(expanded_private_key[:32], "little"), _B))


def ed25519_sign(expanded_private_key: bytes, message: bytes) -> bytes:
    a = int.from_bytes(expanded_private_key[:32], "little")
    public_key = _encode(_mul(a, _B))
    r = _hash(expanded_private_key[32:] + message) % _L
    commitment = _encode(_mul(r, _B))
    s = (r + _hash(commitment + public_key + message) * a) % _L
    return commitment + s.to_bytes(32, "little")


def ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    try:
        a_point, r_point = _decode(public_key), _decode(signature[:32])
    except ValueError:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    k = _hash(signature[:32] + public_key + message) % _L
    return _mul(s, _B) == _add(r_point, _mul(k, a_point))


def parse_advert(packet: bytes) -> Dict[str, Any] | None:
    """The signed advert inside a raw MeshCore packet, or None for any other packet."""
    try:
        header = packet[0]
        if (header >> 2) & 0x0F != ADVERT_PAYLOAD_TYPE:
            return None
        pos = 5 if header & 3 in (0, 3) else 1  # transport routes carry two extra 16-bit codes
        path_byte = packet[pos]
        pos += 1 + (path_byte & 0x3F) * ((path_byte >> 6) + 1)
        payload = packet[pos:]
        public_key, timestamp, signature, app_data = payload[:32], payload[32:36], payload[36:100], payload[100:]
        flags, offset, name = app_data[0], 1, ""
        if flags & 0x10:
            offset += 8
        if flags & 0x20:
            offset += 2
        if flags & 0x40:
            offset += 2
        if flags & 0x80:
            name = app_data[offset:].decode("utf-8", errors="replace")
        return {
            "public_key": public_key.hex(),
            "timestamp": int.from_bytes(timestamp, "little"),
            "type": flags & 0x0F,
            "name": name,
            "verified": len(payload) > 100 and ed25519_verify(public_key, public_key + timestamp + app_data, signature),
        }
    except IndexError:
        return None


class MapUploader:
    """Uploads the repeaters, rooms and sensors whose adverts the radio hears to map.meshcore.io, signed by the radio's key."""

    def __init__(self, radio_info: Dict[str, Any], private_key: bytes, upload=None):
        self.radio_info = radio_info
        self.private_key = private_key
        self.public_key = ed25519_public_key(private_key)
        self.upload = upload or self.post
        self.seen: Dict[str, int] = {}

    @staticmethod
    def post(body: str) -> str:
        request = urllib.request.Request(MAP_UPLOAD_URL, data=body.encode(), method="POST")
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read().decode(errors="replace")

    def build_request(self, packet: bytes) -> str:
        data = json.dumps(
            {
                "params": {
                    "freq": self.radio_info.get("radio_freq"),
                    "cr": self.radio_info.get("radio_cr"),
                    "sf": self.radio_info.get("radio_sf"),
                    "bw": self.radio_info.get("radio_bw"),
                },
                "links": [f"meshcore://{packet.hex()}"],
            },
            separators=(",", ":"),
        )
        signature = ed25519_sign(self.private_key, hashlib.sha256(data.encode()).digest())
        return json.dumps({"data": data, "signature": signature.hex(), "publicKey": self.public_key.hex()})

    async def handle_packet(self, raw_hex: str) -> str | None:
        """Upload the advert in this raw packet if the map should get it; returns the node name when uploaded."""
        packet = bytes.fromhex(raw_hex)
        advert = parse_advert(packet)
        if advert is None or advert["type"] not in MAP_NODE_TYPES or not advert["verified"]:
            return None
        seen = self.seen.get(advert["public_key"])
        if seen is not None and advert["timestamp"] < seen + MAP_REUPLOAD_SECONDS:
            return None
        await asyncio.to_thread(self.upload, self.build_request(packet))
        self.seen[advert["public_key"]] = advert["timestamp"]
        return advert["name"] or advert["public_key"][:12]


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
