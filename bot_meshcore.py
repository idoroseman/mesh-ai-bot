import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Dict, List

from meshcore import EventType, MeshCore

from bot import BOT_MENTIONS, MODEL_NAME, byte_len, generate_bot_reply
from config import CONFIG
from tools_meshcore import MeshCoreTools, MapUploader

# All settings live in config.json
MESHCORE_PORT = CONFIG["meshcore_port"]  # e.g. /dev/cu.usbmodem1101 or COM16
# The radio's Bluetooth address, or part of its name such as "MeshCore-Bot"; when set, it is used instead of the serial port
MESHCORE_BLE = CONFIG.get("meshcore_ble", "")
MESHCORE_DEBUG = CONFIG["meshcore_debug"]
# Upload the repeaters the radio hears to map.meshcore.io; needs the radio to allow exporting its private key
MESHCORE_MAP_UPLOAD = CONFIG.get("meshcore_map_upload", False)

# MeshCore text payloads are limited to ~160 bytes, and Hebrew takes 2 bytes per character.
MAX_MESSAGE_BYTES = CONFIG["max_message_bytes"]
MAX_REPLY_PARTS = CONFIG["max_reply_parts"]
PART_DELAY_SECONDS = CONFIG["part_delay_seconds"]


# Repeater names (or parts of names) to probe; empty means every repeater the radio knows
PROBE_REPEATERS: List[str] = CONFIG.get("meshcore_probe_repeaters", [])
PROBE_INTERVAL_SECONDS = CONFIG.get("meshcore_probe_interval_seconds", 3600)
PROBE_FILE = CONFIG.get("meshcore_probe_file", "repeater_telemetry.jsonl")
# Repeaters answer status requests only after a login; empty means a guest login
REPEATER_PASSWORD = CONFIG.get("meshcore_repeater_password", "")


def select_repeaters(contacts: Dict[str, Dict[str, Any]], names: List[str]) -> List[Dict[str, Any]]:
    wanted = [n.lower() for n in names]
    return [
        c
        for c in contacts.values()
        if c.get("type") == 2 and (not wanted or any(n in c.get("adv_name", "").lower() for n in wanted))
    ]


async def probe_repeater(meshcore: MeshCore, contact: Dict[str, Any]) -> Dict[str, Any]:
    commands = meshcore.commands
    record: Dict[str, Any] = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "repeater": contact.get("adv_name", "")}
    try:
        login = await commands.send_login_sync(contact, REPEATER_PASSWORD)
        record["logged_in"] = login is not None
        record["status"] = await commands.req_status_sync(contact)
        record["telemetry"] = await commands.req_telemetry_sync(contact)
        if not record["status"] and not record["telemetry"]:
            record["error"] = "no answer"
    except Exception as exc:
        record["error"] = repr(exc)
    return record


async def probe_forever(meshcore: MeshCore) -> None:
    """Append one JSON line per repeater to PROBE_FILE every PROBE_INTERVAL_SECONDS."""
    while True:
        try:
            for contact in select_repeaters(meshcore.contacts, PROBE_REPEATERS):
                record = await probe_repeater(meshcore, contact)
                print(f"Probed repeater {record['repeater']}: {record.get('error', 'ok')}")
                with open(PROBE_FILE, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:
            print(f"Repeater probe failed: {exc!r}")
        await asyncio.sleep(PROBE_INTERVAL_SECONDS)


def parse_channel_text(text: str) -> tuple[str, str]:
    """Channel messages arrive as 'SenderName: message'."""
    if ":" in text:
        sender, message = text.split(":", 1)
        return sender.strip(), message.strip()
    return "", text.strip()


def should_answer(text: str, channel_name: str, is_dm: bool, bot_name: str = "") -> bool:
    if not text or not text.strip():
        return False
    if is_dm or "bot" in channel_name.lower():
        return True
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


async def create_map_uploader(meshcore: MeshCore) -> MapUploader | None:
    result = await meshcore.commands.export_private_key()
    if result.type != EventType.PRIVATE_KEY:
        print(f"Map upload disabled, the radio would not export its key: {result.type}")
        return None
    uploader = MapUploader(meshcore.self_info, result.payload["private_key"])
    if uploader.public_key.hex() != meshcore.self_info.get("public_key", uploader.public_key.hex()):
        print("Map upload disabled, the exported key does not match the radio's public key")
        return None
    print("Uploading heard repeaters to map.meshcore.io")
    return uploader


async def main() -> int:
    if not MESHCORE_BLE and not MESHCORE_PORT:
        print("Set meshcore_ble or meshcore_port in config.json to reach your MeshCore companion radio.")
        return 1

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

    tools = MeshCoreTools(meshcore, MODEL_NAME, asyncio.get_running_loop())
    reply_lock = asyncio.Lock()
    tasks: set = set()

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
    if map_uploader:
        subscriptions.append(meshcore.subscribe(EventType.RX_LOG_DATA, upload_to_map))

    try:
        print("Listening...")
        # Keep the program alive
        while True:
            await asyncio.sleep(3600)
    finally:
        probe_task.cancel()
        for subscription in subscriptions:
            meshcore.unsubscribe(subscription)
        await meshcore.stop_auto_message_fetching()
        await meshcore.disconnect()
        print("Disconnected")


if __name__ == "__main__":
    asyncio.run(main())
