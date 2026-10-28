import asyncio
import re
import signal
from typing import Any, Awaitable, Callable, Dict, List

from meshcore import EventType, MeshCore

from bot import BOT_MENTIONS, MODEL_NAME, byte_len, generate_bot_reply
from config import CONFIG
from map_upload import create_map_uploader
from mqtt_observer import create_mqtt_observer
from repeater_telemetry import history, latest_rows, probe_forever
from status_server import STATUS_PORT, StatusServer
from tools_meshcore import MeshCoreTools

# All settings live in config.json
MESHCORE_PORT = CONFIG["meshcore_port"]  # e.g. /dev/cu.usbmodem1101 or COM16
# The radio's Bluetooth address, or part of its name such as "MeshCore-Bot"; when set, it is used instead of the serial port
MESHCORE_BLE = CONFIG.get("meshcore_ble", "")
MESHCORE_DEBUG = CONFIG["meshcore_debug"]
# The bot answers these channels unless another user is mentioned; elsewhere only a bot mention
MESHCORE_CHANNELS = CONFIG.get("meshcore_channels", ["#bot"])
# Upload the repeaters the radio hears to map.meshcore.io; needs the radio to allow exporting its private key
MESHCORE_MAP_UPLOAD = CONFIG.get("meshcore_map_upload", False)
# Report the packets the radio hears to MeshMapper's MQTT broker as an observer; also needs the key export
MESHCORE_MQTT_OBSERVER = CONFIG.get("meshcore_mqtt_observer", False)

# MeshCore text payloads are limited to ~160 bytes, and Hebrew takes 2 bytes per character.
MAX_MESSAGE_BYTES = CONFIG["max_message_bytes"]
MAX_REPLY_PARTS = CONFIG["max_reply_parts"]
PART_DELAY_SECONDS = CONFIG["part_delay_seconds"]


def parse_channel_text(text: str) -> tuple[str, str]:
    """Channel messages arrive as 'SenderName: message'."""
    if ":" in text:
        sender, message = text.split(":", 1)
        return sender.strip(), message.strip()
    return "", text.strip()


def normalize_channel(name: str) -> str:
    return name.strip().lstrip("#").lower()


def should_answer(text: str, channel_name: str, is_dm: bool, bot_name: str = "") -> bool:
    """Answer DMs and monitored channels, except channel messages mentioning someone else."""
    if not text or not text.strip():
        return False
    if is_dm:
        return True
    if normalize_channel(channel_name) in {normalize_channel(c) for c in MESHCORE_CHANNELS}:
        bot_names = {token.lower().removeprefix("@").strip("[]") for token in BOT_MENTIONS}
        if bot_name:
            bot_names.add(bot_name.lower())
        mentions = re.findall(r"(?<![\w@])@(?:\[([^\]\r\n]+)\]|([\w!][\w-]*))", text)
        return all((bracketed or plain).lower() in bot_names for bracketed, plain in mentions)
    lowered = f" {text.lower()} "
    if bot_name and f"@[{bot_name.lower()}]" in lowered:
        return True
    return any(token in lowered for token in BOT_MENTIONS)


async def send_reply(send: Callable[[str], Awaitable[Any]], parts: List[str]) -> None:
    for idx, part in enumerate(parts):
        if idx:
            await asyncio.sleep(PART_DELAY_SECONDS)
        print(f"Replying: {part}")
        result = await send(part)
        if result.type == EventType.ERROR:
            print(f"Error sending reply: {result.payload}")
            return


async def connect() -> MeshCore | None:
    """Connect to the companion radio over Bluetooth if a device is set, otherwise over the serial port."""
    if MESHCORE_BLE:
        print(f"Connecting to MeshCore device {MESHCORE_BLE} over Bluetooth...")
        return await MeshCore.create_ble(MESHCORE_BLE, debug=MESHCORE_DEBUG)
    return await MeshCore.create_serial(MESHCORE_PORT, debug=MESHCORE_DEBUG)



async def main() -> int:
    if not MESHCORE_BLE and not MESHCORE_PORT:
        print("Set meshcore_ble or meshcore_port in config.json to reach your MeshCore companion radio.")
        return 1

    # systemd stops the service with SIGTERM; cancel main so the radio is disconnected cleanly
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    except (NotImplementedError, RuntimeError):
        pass

    link = f"Bluetooth ({MESHCORE_BLE})" if MESHCORE_BLE else MESHCORE_PORT
    try:
        meshcore = await connect()
    except ConnectionError as exc:
        print(f"Error connecting: {exc}")
        meshcore = None
    if meshcore is None:
        print(f"Could not connect to a MeshCore companion on {link}")
        return 1

    bot_name = meshcore.self_info.get("name", "")
    print(f"Connected on {link} as {bot_name!r}")

    await meshcore.ensure_contacts()
    await meshcore.start_auto_message_fetching()

    print("Advertising bot...")
    advert_result = await meshcore.commands.send_advert(flood=True)
    if advert_result.type == EventType.ERROR:
        print(f"Error advertising bot: {advert_result.payload}")

    map_uploader = await create_map_uploader(meshcore) if MESHCORE_MAP_UPLOAD else None
    mqtt_observer = await create_mqtt_observer(meshcore) if MESHCORE_MQTT_OBSERVER else None

    tools = MeshCoreTools(meshcore, MODEL_NAME, asyncio.get_running_loop())
    reply_lock = asyncio.Lock()
    tasks: set[asyncio.Task] = set()

    async def answer_channel_message(msg: Dict[str, Any]) -> None:
        chan = msg.get("channel_idx")
        sender, text = parse_channel_text(msg.get("text", ""))
        channel = await tools.get_channel_name(chan)
        print(f"Received on channel {chan} ({channel}) from {sender}: {text} | path_len={msg.get('path_len')}")
        tools.record("channel message", f"from {sender} on {channel or chan}")
        tools.counters["messages_received"] += 1

        if sender == bot_name or not should_answer(text, channel, False, bot_name):
            return

        # The radio prepends "<bot name>: " to every channel message it sends, and each part mentions the sender
        prefix = f"@[{sender}] " if sender else ""
        max_bytes = MAX_MESSAGE_BYTES - byte_len(bot_name) - 2 - byte_len(prefix)
        metadata = {**msg, "user": sender, "channel": channel or str(chan)}
        async with reply_lock:
            reply = await asyncio.to_thread(generate_bot_reply, text, metadata, max_bytes, MAX_REPLY_PARTS, tools)
            await send_reply(lambda part: meshcore.commands.send_chan_msg(chan, prefix + part), reply)
            tools.counters["replies_sent"] += 1

    async def answer_contact_message(msg: Dict[str, Any]) -> None:
        # txt_type 0 is plain text; skip CLI data and signed messages
        if msg.get("txt_type") != 0:
            return
        prefix = msg.get("pubkey_prefix", "")
        text = msg.get("text", "").strip()
        contact = meshcore.get_contact_by_key_prefix(prefix)
        if contact is None:
            await meshcore.ensure_contacts(follow=True)
            contact = meshcore.get_contact_by_key_prefix(prefix)
        sender = contact.get("adv_name", prefix) if contact else prefix
        print(f"Received direct message from {sender}: {text} | path_len={msg.get('path_len')}")
        # Who messages the bot privately is not shared with other users
        tools.record("direct message")
        tools.counters["messages_received"] += 1

        if not should_answer(text, "", True):
            return

        metadata = {**msg, "user": sender, "channel": "direct"}
        async with reply_lock:
            reply = await asyncio.to_thread(generate_bot_reply, text, metadata, MAX_MESSAGE_BYTES, MAX_REPLY_PARTS, tools)
            await send_reply(lambda part: meshcore.commands.send_msg(contact or prefix, part), reply)
            tools.counters["replies_sent"] += 1

    async def upload_to_map(event) -> None:
        try:
            name = await map_uploader.handle_packet(event.payload.get("payload", ""))
        except Exception as exc:
            print(f"Map upload failed: {exc!r}")
            return
        if name:
            print(f"Uploaded {name} to the map")
            tools.record("map upload", name)

    async def observe_packet(event) -> None:
        try:
            mqtt_observer.handle_packet(event.payload or {})
        except Exception as exc:
            print(f"MQTT observer publish failed: {exc!r}")

    def spawn(handler: Callable[[Dict[str, Any]], Awaitable[None]]):
        # Reply in a background task, with the model in a worker thread, so the event loop stays free meanwhile
        async def on_event(event):
            task = asyncio.create_task(handler(event.payload))
            tasks.add(task)
            task.add_done_callback(log_task_result)

        return on_event

    def log_task_result(task: asyncio.Task) -> None:
        tasks.discard(task)
        if not task.cancelled() and task.exception():
            print(f"Error handling message: {task.exception()!r}")

    def record_event(kind: str, describe: Callable[[Dict[str, Any]], str]):
        async def on_event(event):
            tools.record(kind, describe(event.payload or {}))

        return on_event

    subscriptions = [
        meshcore.subscribe(EventType.CHANNEL_MSG_RECV, spawn(answer_channel_message)),
        meshcore.subscribe(EventType.CONTACT_MSG_RECV, spawn(answer_contact_message)),
        meshcore.subscribe(
            EventType.ADVERTISEMENT, record_event("advert", lambda p: "from " + tools.contact_name(p.get("public_key", "")))
        ),
        meshcore.subscribe(
            EventType.PATH_UPDATE, record_event("path update", lambda p: "for " + tools.contact_name(p.get("public_key", "")))
        ),
        meshcore.subscribe(EventType.NEW_CONTACT, record_event("new contact", lambda p: p.get("adv_name", ""))),
        meshcore.subscribe(EventType.CONNECTED, record_event("radio connected", lambda p: "")),
        meshcore.subscribe(EventType.DISCONNECTED, record_event("radio disconnected", lambda p: str(p.get("reason", "")))),
    ]

    probe_task = asyncio.create_task(probe_forever(meshcore))

    def status_extra() -> Dict[str, Any]:
        extra: Dict[str, Any] = {"repeaters": latest_rows()}
        if mqtt_observer:
            extra["mqtt_observer"] = {
                "connected": mqtt_observer.client.is_connected(),
                "topic": mqtt_observer.packets_topic,
                "packets_published": mqtt_observer.published,
            }
        if map_uploader:
            extra["map_upload"] = {"nodes_uploaded": len(map_uploader.seen)}
        return extra

    status_server = StatusServer(tools, asyncio.get_running_loop(), status_extra, history, bot_name) if STATUS_PORT else None
    if status_server:
        status_server.start()
    if map_uploader:
        subscriptions.append(meshcore.subscribe(EventType.RX_LOG_DATA, upload_to_map))
    if mqtt_observer:
        subscriptions.append(meshcore.subscribe(EventType.RX_LOG_DATA, observe_packet))

    try:
        print("Listening...")
        # Keep the program alive
        while True:
            await asyncio.sleep(3600)
    finally:
        probe_task.cancel()
        if status_server:
            status_server.stop()
        if mqtt_observer:
            mqtt_observer.stop()
        for subscription in subscriptions:
            meshcore.unsubscribe(subscription)
        await meshcore.stop_auto_message_fetching()
        await meshcore.disconnect()
        print("Disconnected")


if __name__ == "__main__":
    asyncio.run(main())
