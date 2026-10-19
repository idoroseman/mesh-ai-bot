import json
import time
from collections import Counter, deque
from functools import partial
from typing import Any, Callable, Dict, List

from config import CONFIG

MAX_BACKGROUND_CHARS = CONFIG["max_background_chars"]
MAX_TOOL_RESULT_CHARS = CONFIG["max_tool_result_chars"]
MAX_LISTED_CONTACTS = CONFIG["max_listed_contacts"]
MAX_NETWORK_EVENTS = CONFIG["max_network_events"]
DEFAULT_MAX_CHANNELS = 8

CONTACT_TYPES = {1: "companion", 2: "repeater", 3: "room", 4: "sensor"}


def tool_spec(name: str, description: str, properties: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": []},
        },
    }


TOOL_SPECS = [
    tool_spec(
        "ping",
        "Call this when the user sends just 'ping'. Reply with exactly the text it returns and nothing else.",
    ),
    tool_spec(
        "link_test",
        "Call this when the user sends a test or בדיקה to check that the bot hears them. Returns how "
        "the user's message was received: sender, channel, path_len (hops; 255 means a direct route), "
        "SNR and timestamps, when available.",
    ),
    tool_spec(
        "list_contacts",
        "Who is on the mesh network: the contacts (nodes, stations, repeaters / ממסרים, room servers, sensors) "
        "the bot's radio knows, most recently heard first, with type, hop count, time since last heard and "
        "location, plus totals per type. Use this for any question about which nodes or repeaters exist, "
        "how many there are, or about one specific contact.",
        {
            "query": {"type": "string", "description": "Only contacts whose name contains this text"},
            "kind": {"type": "string", "description": "Only this type: companion, repeater, room or sensor"},
        },
    ),
    tool_spec("list_channels", "List the mesh channels configured on the bot's radio."),
    tool_spec(
        "recent_network_events",
        "Activity log of what recently happened on the mesh, newest first: adverts, new contacts, path "
        "updates, received messages and radio connects/disconnects. Only for questions about recent "
        "activity; to list the nodes or repeaters on the network use list_contacts.",
        {"limit": {"type": "integer", "description": "How many events to return (default 15)"}},
    ),
    tool_spec(
        "bot_health",
        "Health status of the bot and its radio: uptime, connection, battery, radio settings, signal, "
        "airtime, packet counters and reply/error counts.",
    ),
]


def format_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def dump_bounded(result: Any, limit: int) -> str:
    """Serialize a tool result to valid JSON of at most limit characters.

    An oversized result loses items from the end of its largest list and gains
    "truncated": true, so the output is never cut off in the middle of a value.
    """
    dumped = to_json(result)
    if len(dumped) <= limit:
        return dumped

    if isinstance(result, list):
        result = {"items": result}
    lists = {k: v for k, v in result.items() if isinstance(v, list)} if isinstance(result, dict) else {}
    if lists:
        key = max(lists, key=lambda k: len(to_json(lists[k])))
        items = lists[key]

        def with_items(count: int) -> str:
            return to_json({**result, key: items[:count], "truncated": True})

        # Binary search for the most items that still fit
        low, high = 0, len(items)
        while low < high:
            mid = (low + high + 1) // 2
            if len(with_items(mid)) <= limit:
                low = mid
            else:
                high = mid - 1
        if len(with_items(low)) <= limit:
            return with_items(low)
    return to_json({"error": "result too large", "truncated": True})


class MeshTools:
    """What the tools of every radio share: the event log, the counters, the background and running a tool."""

    specs = TOOL_SPECS
    # Each network has its own community: subclasses point this at its file
    background_path = ""

    def __init__(self, radio: Any, model_name: str):
        self.radio = radio
        self.model_name = model_name
        self.events: deque = deque(maxlen=MAX_NETWORK_EVENTS)
        self.counters: Counter = Counter()
        self.model_supports_tools = True
        self.started = time.time()

    def record(self, kind: str, detail: str = "") -> None:
        print(f"Event {kind}: {detail}")
        self.events.append((time.time(), kind, detail))

    def background(self) -> str:
        """Who the bot serves on this network and its events, for the system prompt; read each time so edits apply live."""
        try:
            with open(self.background_path, "r", encoding="utf-8") as f:
                return f.read().strip()[:MAX_BACKGROUND_CHARS]
        except FileNotFoundError:
            return ""

    def ping(self) -> str:
        return "pong"

    def recent_network_events(self, limit: int = 15) -> List[str]:
        try:
            limit = max(1, min(int(limit), 30))
        except (TypeError, ValueError):
            limit = 15
        now = time.time()
        recent = list(self.events)[-limit:][::-1]
        return [f"{format_age(now - when)} ago: {kind} {detail}".strip() for when, kind, detail in recent]


    @staticmethod
    def to_text(result: Any) -> str:
        return result if isinstance(result, str) else dump_bounded(result, MAX_TOOL_RESULT_CHARS)

    def find_handler(self, name: str, metadata: Dict[str, Any]) -> Callable[..., Any] | None:
        """The method behind a tool name; metadata describes the user message that is being answered."""
        return {
            "ping": self.ping,
            "link_test": partial(self.link_test, metadata),
            "list_contacts": self.list_contacts,
            "list_channels": self.list_channels,
            "recent_network_events": self.recent_network_events,
            "bot_health": self.bot_health,
        }.get(name)

    def call(self, name: str, arguments: Dict[str, Any], metadata: Dict[str, Any]) -> str:
        """Run one tool for the model."""
        print(f"tool name: {name} args: {arguments}, meta:{metadata}")
        handler = self.find_handler(name, metadata)
        if handler is None:
            return f"Unknown tool: {name}"
        try:
            rv = self.to_text(handler(**arguments))
            print(f"tool retuen: {rv}")
            return rv
        except Exception as exc:
            print(f"tool error: {exc}")
            return f"Tool error: {exc}"
