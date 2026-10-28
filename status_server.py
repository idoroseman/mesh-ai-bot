import asyncio
import inspect
import threading
import time
from typing import Any, Callable, Dict, List

from flask import Flask, abort, jsonify, render_template
from werkzeug.serving import make_server

from config import CONFIG
from tools import MeshTools, format_age

import logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# A status page for the running bot; 0 turns it off. It has no login, so keep the host on a trusted network
STATUS_PORT = CONFIG.get("status_port", 8080)
STATUS_HOST = CONFIG.get("status_host", "0.0.0.0")
HEALTH_TIMEOUT_SECONDS = 15
# Tabs that read the radio; the rest come from the bot's own state
HEALTH_TABS = ("bot", "radio")


class StatusServer:
    """Serves the bot's health, counters and recent events over HTTP from a background thread."""

    def __init__(
        self,
        tools: MeshTools,
        loop: asyncio.AbstractEventLoop | None = None,
        extra: Callable[[], Dict[str, Any]] = dict,
        telemetry: Callable[[], List[Dict[str, Any]]] | None = None,
        name: str = "",
    ):
        self.tools = tools
        # The event loop that talks to the radio; async health checks are handed over to it
        self.loop = loop
        # More tabs: each key is a tab holding a table (a list of rows) or a list of facts (a dict)
        self.extra = extra
        # Repeater readings over time for the graphs tab, as repeater_telemetry.history() gives them
        self.telemetry = telemetry
        self.name = name
        self.app = self.create_app()
        self.server = None
        self.thread: threading.Thread | None = None

    def health(self) -> Dict[str, Any]:
        try:
            result = self.tools.bot_health()
            if inspect.isawaitable(result):
                result = asyncio.run_coroutine_threadsafe(result, self.loop).result(HEALTH_TIMEOUT_SECONDS)
            return result
        except Exception as exc:
            # Still show what the bot itself knows when the radio does not answer
            return {
                "bot_uptime": format_age(time.time() - self.tools.started),
                "model": self.tools.model_name,
                "counters": dict(self.tools.counters),
                "error": repr(exc),
            }

    def status(self) -> Dict[str, Any]:
        return {
            "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "health": self.health(),
            "events": self.tools.recent_network_events(30),
            "extra": self.extra(),
        }

    def tabs(self, extra: Dict[str, Any]) -> List[str]:
        return [*HEALTH_TABS, *(key for key, rows in extra.items() if rows), "events", *(["graphs"] if self.telemetry else [])]

    def create_app(self) -> Flask:
        app = Flask(__name__)

        @app.get("/")
        @app.get("/<tab>")
        def page(tab: str = "bot"):
            extra = self.extra()
            tabs = self.tabs(extra)
            if tab not in tabs:
                abort(404)
            context = {
                "tab": tab,
                "tabs": tabs,
                "name": self.name,
                "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "health": self.health() if tab in HEALTH_TABS else {},
                "events": self.tools.recent_network_events(30) if tab == "events" else [],
                "rows": extra.get(tab),
            }
            return render_template("graphs.html" if tab == "graphs" else "tab.html", **context)

        @app.get("/api/status")
        def api_status():
            return jsonify(self.status())

        @app.get("/api/telemetry")
        def api_telemetry():
            if not self.telemetry:
                abort(404)
            return jsonify(self.telemetry())

        return app

    def start(self, host: str = STATUS_HOST, port: int = STATUS_PORT) -> None:
        self.server = make_server(host, port, self.app, threaded=True)
        self.thread = threading.Thread(target=self.server.serve_forever, name="status-server", daemon=True)
        self.thread.start()
        print(f"Status page on http://{host}:{self.server.server_port}/")

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.thread.join(5)
