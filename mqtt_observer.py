import base64
import hashlib
import json
import re
import ssl
import time
from datetime import datetime, timezone
from typing import Any, Dict

import paho.mqtt.client as mqtt
from meshcore import EventType, MeshCore

from config import CONFIG
from map_upload import ed25519_public_key, ed25519_sign, export_private_key, split_packet

# Publish what the radio hears to an observer broker such as MeshMapper's; needs the radio to export its private key
MQTT_SERVER = CONFIG.get("meshcore_mqtt_server", "mqtt.meshmapper.net")
MQTT_PORT = CONFIG.get("meshcore_mqtt_port", 443)
# The region code the broker files this observer under, e.g. "TLV"; it must match the MeshMapper region
MQTT_IATA = CONFIG.get("meshcore_mqtt_iata", "").upper()

CLIENT_VERSION = "mesh-ai-bot/1.0"
TOKEN_SECONDS = 3600
TRACE_PAYLOAD_TYPE = 9


def base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def create_auth_token(private_key: bytes, audience: str, now: float | None = None) -> str:
    """The MeshCore broker password: a JWT signed with the radio's Ed25519 key, as meshcoretomqtt makes it."""
    iat = int(time.time() if now is None else now)
    header = base64url(json.dumps({"alg": "Ed25519", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {
        "publicKey": ed25519_public_key(private_key).hex().upper(),
        "iat": iat,
        "exp": iat + TOKEN_SECONDS,
        "aud": audience,
        "client": CLIENT_VERSION,
    }
    payload = base64url(json.dumps(claims, separators=(",", ":")).encode())
    signature = ed25519_sign(private_key, f"{header}.{payload}".encode())
    return f"{header}.{payload}.{signature.hex().upper()}"


def packet_hash(payload_type: int, path_byte: int, payload: bytes) -> str:
    """The 8-byte packet hash the firmware logs, which lets the map join copies of a packet heard by several observers."""
    data = bytes([payload_type]) + (bytes([path_byte]) if payload_type == TRACE_PAYLOAD_TYPE else b"") + payload
    return hashlib.sha256(data).digest()[:8].hex().upper()


def packet_message(log: Dict[str, Any], origin: str, origin_id: str, now: datetime | None = None) -> Dict[str, Any] | None:
    """A received packet in meshcoretomqtt's packets format, or None when the raw bytes cannot be parsed."""
    try:
        raw = bytes.fromhex(log.get("payload", ""))
        header, path_byte, path, payload = split_packet(raw)
    except (ValueError, IndexError):
        return None
    now = now or datetime.now(timezone.utc)
    local = now.astimezone()
    payload_type = (header >> 2) & 0x0F
    direct = header & 3 in (2, 3)
    message = {
        "origin": origin,
        "origin_id": origin_id,
        "timestamp": now.isoformat(),
        "type": "PACKET",
        "direction": "rx",
        "time": local.strftime("%H:%M:%S"),
        "date": f"{local.day}/{local.month}/{local.year}",
        "len": str(len(raw)),
        "packet_type": str(payload_type),
        "route": "D" if direct else "F",
        "payload_len": str(len(payload)),
        "raw": raw.hex().upper(),
        "SNR": str(int(log["snr"])) if "snr" in log else None,
        "RSSI": str(log["rssi"]) if "rssi" in log else None,
        # The companion radio does not report the firmware's receive score or airtime
        "score": None,
        "duration": None,
        "hash": packet_hash(payload_type, path_byte, payload),
    }
    if direct and path:
        message["path"] = " -> ".join(hop.hex().upper() for hop in path)
    return message


class MqttObserver:
    """Publishes the packets the radio hears to meshcore/<IATA>/<PUBKEY>/packets, with its status beside them."""

    def __init__(self, radio_info: Dict[str, Any], device_info: Dict[str, Any], private_key: bytes, client=None):
        self.private_key = private_key
        self.public_key = ed25519_public_key(private_key).hex().upper()
        self.origin = radio_info.get("name", "")
        self.radio = ",".join(str(radio_info.get(k)) for k in ("radio_freq", "radio_bw", "radio_sf", "radio_cr"))
        self.model = device_info.get("model") or "unknown"
        self.firmware = device_info.get("ver") or "unknown"
        prefix = f"meshcore/{MQTT_IATA}/{self.public_key}"
        self.packets_topic = f"{prefix}/packets"
        self.status_topic = f"{prefix}/status"
        self.published = 0
        self.client = client or self.create_client()

    def create_client(self):
        client_id = re.sub(r"[^a-zA-Z0-9_-]", "", "meshcore_" + self.public_key)[:23]
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, transport="websockets")
        client.ws_set_options(path="/")
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        client.will_set(self.status_topic, json.dumps(self.status_message("offline")), retain=True)
        client.on_connect = self.on_connect
        client.on_disconnect = self.on_disconnect
        self.set_credentials(client)
        return client

    def set_credentials(self, client) -> None:
        client.username_pw_set(f"v1_{self.public_key}", create_auth_token(self.private_key, MQTT_SERVER))

    def status_message(self, status: str) -> Dict[str, Any]:
        return {
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "origin": self.origin,
            "origin_id": self.public_key,
            "radio": self.radio,
            "model": self.model,
            "firmware_version": self.firmware,
            "client_version": CLIENT_VERSION,
        }

    def on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            print(f"MQTT observer refused by {MQTT_SERVER}: {reason_code}")
            return
        print(f"MQTT observer connected to {MQTT_SERVER} as {self.status_topic.rsplit('/', 1)[0]}")
        client.publish(self.status_topic, json.dumps(self.status_message("online")), retain=True)

    def on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        print(f"MQTT observer disconnected: {reason_code}")
        # The token lasts an hour, so the automatic reconnect needs a fresh one
        self.set_credentials(client)

    def start(self) -> None:
        self.client.connect_async(MQTT_SERVER, MQTT_PORT, keepalive=60)
        self.client.loop_start()

    def stop(self) -> None:
        # A clean disconnect skips the will, so say offline first
        self.client.publish(self.status_topic, json.dumps(self.status_message("offline")), retain=True)
        self.client.disconnect()
        self.client.loop_stop()

    def handle_packet(self, log: Dict[str, Any]) -> bool:
        print(f"Sending packet to mqtt {self.published}")
        """Publish one RX_LOG_DATA payload; returns whether it was handed to the client."""
        message = packet_message(log, self.origin, self.public_key)
        if message is None or not self.client.is_connected():
            return False
        self.client.publish(self.packets_topic, json.dumps(message))
        self.published += 1
        return True


async def create_mqtt_observer(meshcore: MeshCore) -> MqttObserver | None:
    if not MQTT_IATA:
        print("MQTT observer disabled, set meshcore_mqtt_iata to your MeshMapper region code")
        return None
    private_key = await export_private_key(meshcore, "MQTT observer")
    if private_key is None:
        print("Failed to export private key for MQTT observer")
        return None
    device = await meshcore.commands.send_device_query()
    device_info = device.payload if device.type != EventType.ERROR else {}
    observer = MqttObserver(meshcore.self_info, device_info, private_key)
    observer.start()
    print("MQTT observer started")
    return observer
