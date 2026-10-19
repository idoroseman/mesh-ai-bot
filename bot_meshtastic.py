import time
import meshtastic
import meshtastic.serial_interface
from google.protobuf.json_format import ParseDict
from meshtastic.ble_interface import BLEInterface
from meshtastic.protobuf import mesh_pb2, portnums_pb2
from pubsub import pub

from config import CONFIG
from bot import BOT_MENTIONS, MODEL_NAME, byte_len, generate_bot_reply

MESHTASTIC_PORT = CONFIG["meshtastic_port"]  # e.g. /dev/cu.usbmodem1101 or COM16
# The radio's Bluetooth name or address, e.g. "Meshtastic_6b4e"; when set, it is used instead of the serial port
MESHTASTIC_BLE = CONFIG.get("meshtastic_ble", "")

MAX_MESSAGE_BYTES = CONFIG["max_message_bytes"]
MAX_REPLY_PARTS = CONFIG["max_reply_parts"]
PART_DELAY_SECONDS = CONFIG["part_delay_seconds"]

from tools_meshtastic import MeshtasticTools


def mentions_bot(text, bot_id):
    """On a channel the bot only answers messages that call it, e.g. '@bot' or '@!e9d66b4e'."""
    lowered = f" {text.lower()} "
    # The Meshtastic apps write a mention as '@' followed by the node id
    return f"@{bot_id}" in lowered or any(token in lowered for token in BOT_MENTIONS)


def on_receive_packet(packet, interface):
    """Callback function triggered whenever a packet is received."""
    global tools
    try:
        # Check if the packet contains a text message
        channel = packet.get("channel", 0)
        sender_id = packet.get('fromId')
        portnum = packet.get("decoded", {}).get("portnum")
        from_node = packet.get("fromId", "?")

        if portnum != 'TEXT_MESSAGE_APP':
            return
        print(f"💬 Processing TEXT_MESSAGE_APP on channel {channel}")
        if channel != 0:
            return
        
        # Avoid responding to our own messages or empty strings
        if packet.get('from') == interface.myInfo.my_node_num:
            return

        message_text = packet['decoded'].get('text', '')
        # A message addressed to the bot's node is direct; anything else was sent to the channel
        is_direct = packet.get('to') == interface.myInfo.my_node_num
        bot_id = f"!{interface.myInfo.my_node_num:08x}"
        if not is_direct and not mentions_bot(message_text, bot_id):
            return

        print(f"📥 Received from {sender_id}: {message_text}")

        metadata = packet
        # On the channel the reply starts with a mention of the sender, written like the apps do: '@!<node id>'
        mention = "" if is_direct else f"@!{packet['from']:08x} "
        ai_reply = generate_bot_reply(message_text, metadata, MAX_MESSAGE_BYTES- len(mention), MAX_REPLY_PARTS, tools)
        for idx, part in enumerate(ai_reply):
            if idx:
                time.sleep(PART_DELAY_SECONDS)
            print(f"📤 Replying: {part}")
            if is_direct:
                interface.sendText(text=part, destinationId=packet['from'])
            else:
                # Asked on the channel, so answer on the channel
                interface.sendText(text=mention + part, channelIndex=channel)
        
    except Exception as e:
        print(f"❌ Error processing packet: {e}")


def main():
    global tools

    try:
        interface = None
        if MESHTASTIC_BLE:
            print(f"🔌 Connecting to Meshtastic device {MESHTASTIC_BLE} over Bluetooth...")
            interface = BLEInterface(MESHTASTIC_BLE)
        elif MESHTASTIC_PORT:
            print(f"🔌 Connecting to Meshtastic device on {MESHTASTIC_PORT}...")
            interface = meshtastic.serial_interface.SerialInterface(devPath=MESHTASTIC_PORT)
        else:
            raise RuntimeError("No Meshtastic connection method specified.")

        local_user = (interface.getMyNodeInfo() or {}).get("user", {})
        local_id = local_user.get("id") or f"!{interface.myInfo.my_node_num:08x}"
        print(f"📻 Local node: {local_user.get('longName', 'unknown')} ({local_id})")

        # Announce the radio on the mesh right away, instead of waiting for its next periodic node info
        if local_user:
            announcement = ParseDict(
                {k: v for k, v in local_user.items() if k != "raw"}, mesh_pb2.User(), ignore_unknown_fields=True
            )
            interface.sendData(announcement, portNum=portnums_pb2.PortNum.NODEINFO_APP)
            print("📣 Node info broadcast sent.")

        tools = MeshtasticTools(interface, MODEL_NAME)

        # Subscribe to incoming data packets
        pub.subscribe(on_receive_packet, "meshtastic.receive")
        print("🟢 Bridge active. Listening for mesh messages... Press Ctrl+C to exit.")
        
        # Keep the main thread alive
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n🛑 Shutting down bridge...")
    finally:
        if 'interface' in locals():
            interface.close()

if __name__ == "__main__":
    main()