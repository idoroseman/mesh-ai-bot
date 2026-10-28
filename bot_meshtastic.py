import time
from typing import Any, Dict

import meshtastic.ble_interface
import meshtastic.serial_interface
from google.protobuf.json_format import ParseDict
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import mesh_pb2, portnums_pb2
from pubsub import pub

from bot import BOT_MENTIONS, MODEL_NAME, generate_bot_reply
from config import CONFIG
from tools_meshtastic import MeshtasticTools, node_id_of

MESHTASTIC_PORT = CONFIG["meshtastic_port"]  # e.g. /dev/cu.usbmodem1101 or COM16
# The radio's Bluetooth name or address, e.g. "Meshtastic_6b4e"; when set, it is used instead of the serial port
MESHTASTIC_BLE = CONFIG.get("meshtastic_ble", "")

MAX_MESSAGE_BYTES = CONFIG["max_message_bytes"]
MAX_REPLY_PARTS = CONFIG["max_reply_parts"]
PART_DELAY_SECONDS = CONFIG["part_delay_seconds"]

# Set by main() once the radio is connected; the receive callback needs it
tools: MeshtasticTools | None = None


def mentions_bot(text: str, bot_id: str) -> bool:
    """On a channel the bot only answers messages that call it, e.g. '@bot' or '@!e9d66b4e'."""
    lowered = f" {text.lower()} "
    # The Meshtastic apps write a mention as '@' followed by the node id
    return f"@{bot_id}" in lowered or any(token in lowered for token in BOT_MENTIONS)


def on_receive_packet(packet: Dict[str, Any], interface: MeshInterface) -> None:
    """Answer text messages sent to the bot directly, or mentioning it on the primary channel."""
    try:
        if packet.get("decoded", {}).get("portnum") != "TEXT_MESSAGE_APP":
            return
        channel = packet.get("channel", 0)
        my_num = interface.myInfo.my_node_num
        # Only the primary channel, and never the bot's own messages
        if channel != 0 or packet.get("from") == my_num:
            return

        text = packet["decoded"].get("text", "")
        sender = tools.node_name(node_id_of(packet))
        # A message addressed to the bot's node is direct; anything else was sent to the channel
        is_direct = packet.get("to") == my_num
        channel_name = "direct" if is_direct else tools.channel_name(channel)
        print(f"📥 Received on {channel_name} from {sender}: {text}")
        # Who messages the bot privately is not shared with other users
        if is_direct:
            tools.record("direct message")
        else:
            tools.record("channel message", f"from {sender} on {channel_name}")
        tools.counters["messages_received"] += 1

        if not is_direct and not mentions_bot(text, f"!{my_num:08x}"):
            return

        metadata = {**packet, "user": sender, "channel": channel_name}
        # On the channel the reply starts with a mention of the sender, written like the apps do: '@!<node id>'
        mention = "" if is_direct else f"@{node_id_of(packet)} "
        reply = generate_bot_reply(text, metadata, MAX_MESSAGE_BYTES - len(mention), MAX_REPLY_PARTS, tools)
        for idx, part in enumerate(reply):
            if idx:
                time.sleep(PART_DELAY_SECONDS)
            print(f"📤 Replying: {part}")
            if is_direct:
                interface.sendText(text=part, destinationId=packet["from"])
            else:
                interface.sendText(text=mention + part, channelIndex=channel)
        tools.counters["replies_sent"] += 1
    except Exception as exc:
        print(f"❌ Error processing packet: {exc!r}")


def connect() -> MeshInterface:
    """Connect to the radio over Bluetooth if a device is set, otherwise over the serial port."""
    if MESHTASTIC_BLE:
        print(f"🔌 Connecting to Meshtastic device {MESHTASTIC_BLE} over Bluetooth...")
        return meshtastic.ble_interface.BLEInterface(MESHTASTIC_BLE)
    print(f"🔌 Connecting to Meshtastic device on {MESHTASTIC_PORT}...")
    return meshtastic.serial_interface.SerialInterface(devPath=MESHTASTIC_PORT)


def announce(interface: MeshInterface, user: Dict[str, Any]) -> None:
    """Broadcast the radio's node info right away, instead of waiting for its next periodic one."""
    node_info = ParseDict({k: v for k, v in user.items() if k != "raw"}, mesh_pb2.User(), ignore_unknown_fields=True)
    interface.sendData(node_info, portNum=portnums_pb2.PortNum.NODEINFO_APP)
    print("📣 Node info broadcast sent.")


def main() -> int:
    global tools

    if not MESHTASTIC_BLE and not MESHTASTIC_PORT:
        print("Set meshtastic_ble or meshtastic_port in config.json to reach your Meshtastic radio.")
        return 1

    interface = connect()
    try:
        local_user = (interface.getMyNodeInfo() or {}).get("user", {})
        local_id = local_user.get("id") or f"!{interface.myInfo.my_node_num:08x}"
        print(f"📻 Local node: {local_user.get('longName', 'unknown')} ({local_id})")
        if local_user:
            announce(interface, local_user)

        tools = MeshtasticTools(interface, MODEL_NAME)
        pub.subscribe(on_receive_packet, "meshtastic.receive")
        print("🟢 Listening for mesh messages... Press Ctrl+C to exit.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n🛑 Shutting down...")
    finally:
        interface.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
