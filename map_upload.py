import asyncio
import hashlib
import json
import urllib.request
from typing import Any, Dict, List, Tuple

from meshcore import EventType, MeshCore

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


def split_packet(packet: bytes) -> Tuple[int, int, List[bytes], bytes]:
    """A raw MeshCore packet's header byte, path byte, path hop hashes and payload; raises IndexError when truncated."""
    header = packet[0]
    pos = 5 if header & 3 in (0, 3) else 1  # transport routes carry two extra 16-bit codes
    path_byte = packet[pos]
    hash_size = (path_byte >> 6) + 1
    hops = path_byte & 0x3F
    if len(packet) < pos + 1 + hops * hash_size:
        raise IndexError("packet shorter than its path")
    path = [packet[pos + 1 + i * hash_size : pos + 1 + (i + 1) * hash_size] for i in range(hops)]
    return header, path_byte, path, packet[pos + 1 + hops * hash_size :]


def parse_advert(packet: bytes) -> Dict[str, Any] | None:
    """The signed advert inside a raw MeshCore packet, or None for any other packet."""
    try:
        header, _, _, payload = split_packet(packet)
        if (header >> 2) & 0x0F != ADVERT_PAYLOAD_TYPE:
            return None
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


async def export_private_key(meshcore: MeshCore, purpose: str) -> bytes | None:
    """The radio's expanded Ed25519 private key, checked against its public key, or None when it will not share it."""
    result = await meshcore.commands.export_private_key()
    if result.type != EventType.PRIVATE_KEY:
        print(f"{purpose} disabled, the radio would not export its key: {result.type}")
        return None
    private_key = result.payload["private_key"]
    public_key = ed25519_public_key(private_key).hex()
    if public_key != meshcore.self_info.get("public_key", public_key):
        print(f"{purpose} disabled, the exported key does not match the radio's public key")
        return None
    return private_key


async def create_map_uploader(meshcore: MeshCore) -> MapUploader | None:
    private_key = await export_private_key(meshcore, "Map upload")
    if private_key is None:
        return None
    print("Uploading heard repeaters to map.meshcore.io")
    return MapUploader(meshcore.self_info, private_key)
