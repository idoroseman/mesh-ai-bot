"""Tests for the bot, run against a fake radio and a fake model.

Run with: .venv/bin/python -m unittest -v
"""

import asyncio
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import ollama
from meshcore import EventType
from meshtastic.protobuf import channel_pb2, config_pb2

import bot
import bot_meshcore
import bot_meshtastic
import map_upload
import mqtt_observer
import repeater_telemetry
import status_server
import tools
import tools_meshcore
from tools import dump_bounded, format_age
from tools_meshcore import MeshCoreTools
from tools_meshtastic import MeshtasticTools

KEY_A = "aa" * 32
KEY_B = "bb" * 32
METADATA = {
    "user": "4X1ABC",
    "channel": "bot-il",
    "text": "ping",
    "path_len": 2,
    "SNR": 7.5,
    "sender_timestamp": 1790000000,
}


def ok(payload=None):
    return SimpleNamespace(type=EventType.OK, payload=payload or {})


def error(reason="failed"):
    return SimpleNamespace(type=EventType.ERROR, payload={"reason": reason})


class FakeCommands:
    def __init__(self):
        self.channels = ["Public", "bot-il", ""]
        self.channel_messages = []
        self.direct_messages = []
        self.adverts = []
        self.send_result = ok()

    async def send_advert(self, flood=False):
        self.adverts.append(flood)
        return self.send_result

    async def get_channel(self, idx):
        if idx >= len(self.channels):
            return error()
        return ok({"channel_idx": idx, "channel_name": self.channels[idx], "channel_secret": b"secret"})

    async def send_device_query(self):
        return ok({"max_channels": 8, "model": "Heltec V3", "ver": "1.14", "fw_build": "06-Oct", "ble_pin": 123456})

    async def get_stats_core(self):
        return ok({"battery_mv": 4100, "uptime_secs": 999, "errors": 0, "queue_len": 0})

    async def get_stats_radio(self):
        return error()

    async def get_stats_packets(self):
        return ok({"recv": 5, "sent": 2})

    async def send_chan_msg(self, chan, text):
        self.channel_messages.append((chan, text))
        return self.send_result

    async def send_msg(self, destination, text):
        self.direct_messages.append((destination, text))
        return self.send_result


class FakeMeshCore:
    def __init__(self):
        self.commands = FakeCommands()
        self.is_connected = True
        self.self_info = {
            "name": "ClubBot",
            "radio_freq": 869.618,
            "radio_bw": 62.5,
            "radio_sf": 8,
            "radio_cr": 8,
            "tx_power": 22,
        }
        self.contacts = {
            KEY_A: {
                "public_key": KEY_A,
                "adv_name": "4X1ABC",
                "type": 1,
                "out_path_len": 2,
                "last_advert": int(time.time()) - 300,
                "adv_lat": 32.08,
                "adv_lon": 34.78,
            },
            KEY_B: {
                "public_key": KEY_B,
                "adv_name": "Carmel RPT",
                "type": 2,
                "out_path_len": -1,
                "last_advert": 0,
                "adv_lat": 0,
                "adv_lon": 0,
            },
        }
        self.subscribers = {}
        self.disconnected = False

    async def ensure_contacts(self, follow=False):
        return False

    async def start_auto_message_fetching(self):
        pass

    async def stop_auto_message_fetching(self):
        pass

    async def disconnect(self):
        self.disconnected = True

    def subscribe(self, event_type, callback):
        self.subscribers.setdefault(event_type, []).append(callback)
        return (event_type, callback)

    def unsubscribe(self, subscription):
        event_type, callback = subscription
        self.subscribers[event_type].remove(callback)

    def get_contact_by_key_prefix(self, prefix):
        return next((c for key, c in self.contacts.items() if key.startswith(prefix)), None)

    async def emit(self, event_type, payload):
        for callback in list(self.subscribers.get(event_type, [])):
            await callback(SimpleNamespace(type=event_type, payload=payload))


def tool_request(name, **arguments):
    """A model response that asks for one tool call."""
    call = SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))
    return SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[call]))


def answer(text):
    """A model response with a final text answer."""
    return SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None))


class FakeOllama:
    """Returns scripted responses in order and remembers every chat request."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class QuietTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = mock.patch("builtins.print")
        patcher.start()
        self.addCleanup(patcher.stop)


class TextHelpersTest(unittest.TestCase):
    def test_split_keeps_every_part_within_the_byte_limit(self):
        text = "שלום לכולם זה מבחן ארוך של פיצול הודעות ברשת " * 4
        parts = bot.split_for_mesh(text, 40, 10)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(bot.byte_len(part) <= 40 for part in parts))
        self.assertEqual(" ".join(parts), bot.normalize_text(text))

    def test_split_truncates_with_ellipsis_when_over_max_parts(self):
        parts = bot.split_for_mesh("word " * 100, 20, 2)
        self.assertEqual(len(parts), 2)
        self.assertTrue(parts[-1].endswith("…"))
        self.assertLessEqual(bot.byte_len(parts[-1]), 20)

    def test_split_breaks_a_word_longer_than_one_message(self):
        parts = bot.split_for_mesh("a" * 50, 20, 5)
        self.assertEqual(parts, ["a" * 20, "a" * 20, "a" * 10])

    def test_parse_channel_text(self):
        self.assertEqual(bot_meshcore.parse_channel_text("4X1ABC: hello: there"), ("4X1ABC", "hello: there"))
        self.assertEqual(bot_meshcore.parse_channel_text("no sender"), ("", "no sender"))

    def test_should_answer(self):
        with mock.patch.object(bot_meshcore, "MESHCORE_CHANNELS", ["#bot"]):
            self.assertTrue(bot_meshcore.should_answer("hi", "", True))
            self.assertTrue(bot_meshcore.should_answer("hi", "#bot", False))
            self.assertTrue(bot_meshcore.should_answer("hi", "Bot", False))
            self.assertTrue(bot_meshcore.should_answer("hey @bot status", "Public", False))
            self.assertTrue(bot_meshcore.should_answer("@בוט מה נשמע", "Public", False))
            self.assertTrue(bot_meshcore.should_answer("@[ClubBot] hi", "Public", False, "ClubBot"))
            self.assertFalse(bot_meshcore.should_answer("hello everyone", "Public", False, "ClubBot"))
            self.assertFalse(bot_meshcore.should_answer("is this bot working", "Public", False))
            self.assertFalse(bot_meshcore.should_answer("hi", "#bot-il", False))
            self.assertFalse(bot_meshcore.should_answer("hi", "", False))
        self.assertFalse(bot_meshcore.should_answer("   ", "", True))

    def test_monitored_channel_ignores_mentions_of_other_users(self):
        with mock.patch.object(bot_meshcore, "MESHCORE_CHANNELS", ["#bot"]):
            for text in ("@alice hello", "hello @[Alice Smith]", "@משה שלום", "@!0a0b0c0d hi", "@botman hi", "@bot ask @alice"):
                with self.subTest(text=text):
                    self.assertFalse(bot_meshcore.should_answer(text, "#bot", False, "ClubBot"))
            for text in ("hello everyone", "@bot hi", "@בוט שלום", "@[ClubBot] hi", "@clubbot hi", "email alice@example.com"):
                with self.subTest(text=text):
                    self.assertTrue(bot_meshcore.should_answer(text, "#bot", False, "ClubBot"))
            self.assertTrue(bot_meshcore.should_answer("ask @alice", "", True, "ClubBot"))

    def test_format_age(self):
        self.assertEqual(format_age(-5), "0s")
        self.assertEqual(format_age(59), "59s")
        self.assertEqual(format_age(60), "1m")
        self.assertEqual(format_age(2 * 3600 + 5), "2h")
        self.assertEqual(format_age(3 * 86400), "3d")

    def test_system_prompt_names_user_channel_and_limit(self):
        prompt = bot.build_system_prompt({"user": "4X1ABC", "channel": "bot-il"}, 180)
        self.assertIn("4X1ABC on bot-il", prompt)
        self.assertIn("at most 180 characters", prompt)
        self.assertIn("an unknown user on direct", bot.build_system_prompt({}, 180))


    def test_system_prompt_uses_the_configured_language(self):
        self.assertIn(f"{bot.CONFIG['bot_language']}-speaking", bot.build_system_prompt({}, 180))
        with mock.patch.object(bot, "BOT_LANGUAGE", "Spanish"):
            prompt = bot.build_system_prompt({}, 180)
        self.assertIn("Spanish-speaking", prompt)
        self.assertIn("Prefer Spanish in conversation", prompt)
        self.assertNotIn("Hebrew", prompt)

    def test_system_prompt_includes_the_network_background(self):
        self.assertIn("MeshCore community", bot.build_system_prompt({}, 180, "You serve the MeshCore community."))

    def test_each_network_has_its_own_background(self):
        meshcore = MeshCoreTools(FakeMeshCore(), "test-model")
        meshtastic = MeshtasticTools(SimpleNamespace(), "test-model")
        self.assertIn("meshcore.org.il", meshcore.background())
        self.assertNotIn("meshcore.org.il", meshtastic.background())
        with mock.patch.object(meshcore, "background_path", "missing.md"):
            self.assertEqual(meshcore.background(), "")


class MeshToolsTest(QuietTestCase):
    def setUp(self):
        super().setUp()
        self.mesh = FakeMeshCore()
        self.tools = MeshCoreTools(self.mesh, "test-model")

    def test_every_advertised_tool_is_implemented(self):
        for spec in tools.TOOL_SPECS:
            self.assertTrue(callable(getattr(self.tools, spec["function"]["name"])))

    async def test_list_contacts_sorts_by_last_heard(self):
        result = await self.tools.list_contacts()
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["by_type"], {"companion": 1, "repeater": 1})
        first, second = result["contacts"]
        self.assertEqual(first, {
            "name": "4X1ABC", "type": "companion", "hops": 2, "heard": "5m ago", "location": [32.08, 34.78],
        })
        self.assertEqual(second, {"name": "Carmel RPT", "type": "repeater", "hops": "unknown", "heard": "never"})

    async def test_list_contacts_filters(self):
        self.assertEqual((await self.tools.list_contacts(kind="Repeater"))["matching"], 1)
        self.assertEqual((await self.tools.list_contacts(query="4x1"))["contacts"][0]["name"], "4X1ABC")
        self.assertEqual((await self.tools.list_contacts(query=None, kind=None))["matching"], 2)
        self.assertEqual((await self.tools.list_contacts(query="nobody"))["contacts"], [])

    async def test_list_contacts_caps_the_list(self):
        for i in range(tools.MAX_LISTED_CONTACTS + 10):
            self.mesh.contacts[f"{i:064x}"] = {"adv_name": f"node{i}", "type": 1, "last_advert": i + 1}
        result = await self.tools.list_contacts()
        self.assertEqual(len(result["contacts"]), tools.MAX_LISTED_CONTACTS)
        self.assertEqual(result["matching"], tools.MAX_LISTED_CONTACTS + 12)

    async def test_list_channels_skips_empty_slots_and_hides_secrets(self):
        channels = await self.tools.list_channels()
        self.assertEqual(channels, [{"index": 0, "name": "Public"}, {"index": 1, "name": "bot-il"}])

    async def test_get_channel_name_is_cached(self):
        self.assertEqual(await self.tools.get_channel_name(1), "bot-il")
        self.mesh.commands.channels[1] = "renamed"
        self.assertEqual(await self.tools.get_channel_name(1), "bot-il")
        self.assertEqual(await self.tools.get_channel_name(9), "")

    async def test_recent_network_events_newest_first(self):
        self.tools.record("advert", "from 4X1ABC")
        self.tools.record("direct message")
        self.assertEqual(self.tools.recent_network_events(), ["0s ago: direct message", "0s ago: advert from 4X1ABC"])
        self.assertEqual(len(self.tools.recent_network_events(limit="1")), 1)
        self.assertEqual(len(self.tools.recent_network_events(limit="many")), 2)

    async def test_events_are_bounded(self):
        for i in range(tools.MAX_NETWORK_EVENTS + 20):
            self.tools.record("advert", str(i))
        self.assertEqual(len(self.tools.events), tools.MAX_NETWORK_EVENTS)

    async def test_bot_health(self):
        self.tools.counters["replies_sent"] += 3
        health = await self.tools.bot_health()
        self.assertTrue(health["radio_connected"])
        self.assertEqual(health["model"], "test-model")
        self.assertEqual(health["contacts"], 2)
        self.assertEqual(health["counters"], {"replies_sent": 3})
        self.assertEqual(health["radio"]["freq_mhz"], 869.618)
        self.assertEqual(health["core"]["battery_mv"], 4100)
        self.assertEqual(health["firmware"], {"model": "Heltec V3", "ver": "1.14", "fw_build": "06-Oct"})
        # The radio stats query failed, so that section is left out instead of invented
        self.assertNotIn("signal", health)

    async def test_bot_health_does_not_leak_the_ble_pin(self):
        self.assertNotIn("123456", await self.tools.call_async("bot_health", {}, METADATA))

    async def test_link_test_reports_reception_without_the_text(self):
        details = self.tools.link_test(METADATA)
        self.assertNotIn("text", details)
        self.assertEqual(details["path_len"], 2)
        self.assertEqual(details["SNR"], 7.5)
        self.assertIn("received_at", details)
        self.assertIn("sent_at", details)

    async def test_background_reads_the_file_each_time(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "background.md")
            with mock.patch.object(self.tools, "background_path", path):
                self.assertEqual(self.tools.background(), "")
                with open(path, "w", encoding="utf-8") as f:
                    f.write("10/10/2026 - יום שדה\n")
                self.assertEqual(self.tools.background(), "10/10/2026 - יום שדה")

    async def test_contact_name_falls_back_to_key_prefix(self):
        self.assertEqual(self.tools.contact_name(KEY_A), "4X1ABC")
        self.assertEqual(self.tools.contact_name("cc" * 32), "cc" * 6)

    async def test_call_handles_bad_requests(self):
        self.assertEqual(await self.tools.call_async("nope", {}, METADATA), "Unknown tool: nope")
        self.assertTrue((await self.tools.call_async("list_channels", {"extra": 1}, METADATA)).startswith("Tool error:"))

    async def test_call_truncates_long_results(self):
        for i in range(200):
            self.mesh.contacts[f"{i:064x}"] = {"adv_name": "long name " * 3 + str(i), "type": 1, "last_advert": 1}
        with mock.patch.object(tools_meshcore, "MAX_LISTED_CONTACTS", 200):
            result = await self.tools.call_async("list_contacts", {}, METADATA)
        self.assertLessEqual(len(result), tools.MAX_TOOL_RESULT_CHARS)
        parsed = json.loads(result)
        self.assertTrue(parsed["truncated"])
        self.assertEqual(parsed["matching"], 202)
        self.assertGreater(len(parsed["contacts"]), 0)
        self.assertLess(len(parsed["contacts"]), 200)


class DumpBoundedTest(unittest.TestCase):
    def test_small_results_are_unchanged(self):
        self.assertEqual(dump_bounded({"a": [1, 2], "b": "ג"}, 100), '{"a":[1,2],"b":"ג"}')
        self.assertEqual(dump_bounded([1, 2], 100), "[1,2]")

    def test_oversized_results_stay_valid_json_at_every_limit(self):
        result = {"total": 50, "contacts": [{"name": f"node {i}", "note": 'quote " and ] bracket'} for i in range(50)]}
        # The smallest supported limit is the size of the "result too large" fallback
        for limit in range(50, 400, 7):
            dumped = dump_bounded(result, limit)
            self.assertLessEqual(len(dumped), limit)
            parsed = json.loads(dumped)
            self.assertTrue(parsed["truncated"])
            kept = parsed.get("contacts", [])
            self.assertEqual(kept, result["contacts"][: len(kept)])

    def test_keeps_as_many_items_as_fit(self):
        result = {"contacts": ["x" * 10] * 20}
        kept = len(json.loads(dump_bounded(result, 100))["contacts"])
        self.assertEqual(kept, 5)

    def test_trims_the_largest_list(self):
        parsed = json.loads(dump_bounded({"small": [1, 2], "big": ["x" * 20] * 30}, 120))
        self.assertEqual(parsed["small"], [1, 2])
        self.assertLess(len(parsed["big"]), 30)

    def test_top_level_list_is_wrapped_when_truncated(self):
        parsed = json.loads(dump_bounded(["event " + str(i) for i in range(100)], 100))
        self.assertTrue(parsed["truncated"])
        self.assertEqual(parsed["items"][0], "event 0")

    def test_result_with_nothing_to_trim(self):
        self.assertEqual(json.loads(dump_bounded({"a": "x" * 500}, 100)), {"error": "result too large", "truncated": True})
        self.assertEqual(json.loads(dump_bounded("x" * 500, 100)), {"error": "result too large", "truncated": True})


class ModelReplyTest(QuietTestCase):
    async def asyncSetUp(self):
        self.tools = MeshCoreTools(FakeMeshCore(), "test-model", asyncio.get_running_loop())

    async def generate(self, text, max_bytes, max_parts):
        # The model runs in a worker thread, like in the bot, and its tool calls come back to this loop
        return await asyncio.to_thread(bot.generate_bot_reply, text, METADATA, max_bytes, max_parts, self.tools)

    async def reply(self, client, text="status"):
        with mock.patch.object(bot, "ollama_client", client):
            return " ".join(await self.generate(text, 200, 3))

    async def test_plain_answer(self):
        client = FakeOllama(answer("  73 de\nClubBot  "))
        self.assertEqual(await self.reply(client), "73 de ClubBot")
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.requests[0]["tools"], tools.TOOL_SPECS)
        self.assertEqual(client.requests[0]["messages"][-1], {"role": "user", "content": "status"})

    async def test_tool_result_is_fed_back_to_the_model(self):
        client = FakeOllama(tool_request("list_channels"), answer("Public, bot-il"))
        self.assertEqual(await self.reply(client), "Public, bot-il")
        tool_message = client.requests[1]["messages"][-1]
        self.assertEqual(tool_message["role"], "tool")
        self.assertEqual(tool_message["tool_name"], "list_channels")
        self.assertIn("bot-il", tool_message["content"])

    async def test_ping_answers_pong(self):
        client = FakeOllama(tool_request("ping"), answer("pong"))
        self.assertEqual(await self.reply(client, "ping"), "pong")
        self.assertEqual(client.requests[1]["messages"][-1]["content"], "pong")

    async def test_link_test_gets_the_message_metadata(self):
        client = FakeOllama(tool_request("link_test"), answer("ok"))
        await self.reply(client, "test")
        self.assertNotIn("path_len", client.requests[0]["messages"][0]["content"])
        self.assertIn('"path_len":2', client.requests[1]["messages"][-1]["content"])

    async def test_tool_arguments_are_passed_through(self):
        client = FakeOllama(tool_request("list_contacts", kind="repeater"), answer("ok"))
        await self.reply(client)
        content = client.requests[1]["messages"][-1]["content"]
        self.assertIn("Carmel RPT", content)
        self.assertNotIn('"name":"4X1ABC"', content)

    async def test_stops_after_max_tool_rounds(self):
        client = FakeOllama(*[tool_request("bot_health")] * bot.MAX_TOOL_ROUNDS, answer("final"))
        self.assertEqual(await self.reply(client), "final")
        self.assertEqual(len(client.requests), bot.MAX_TOOL_ROUNDS + 1)
        self.assertNotIn("tools", client.requests[-1])

    async def test_falls_back_when_model_has_no_tool_support(self):
        unsupported = ollama.ResponseError("registry.ollama.ai/library/x does not support tools", 400)
        client = FakeOllama(unsupported, answer("fallback"), answer("again"))
        self.assertEqual(await self.reply(client), "fallback")
        self.assertFalse(self.tools.model_supports_tools)
        system = client.requests[1]["messages"][0]["content"]
        self.assertNotIn("tools", client.requests[1])
        for spec in tools.TOOL_SPECS:
            self.assertIn(spec["function"]["name"] + ":", system)
        # Later messages skip the tool attempt entirely
        self.assertEqual(await self.reply(client), "again")
        self.assertEqual(len(client.requests), 3)

    async def test_model_failure_gives_error_reply(self):
        self.assertEqual(await self.reply(FakeOllama(RuntimeError("down"))), bot.ERROR_REPLY)
        self.assertEqual(await self.reply(FakeOllama(ollama.ResponseError("boom", 500))), bot.ERROR_REPLY)
        self.assertEqual(self.tools.counters["model_errors"], 2)
        self.assertTrue(self.tools.model_supports_tools)

    async def test_reply_comes_back_as_parts_that_fit_a_message(self):
        with mock.patch.object(bot, "ollama_client", FakeOllama(answer("מילה " * 100))):
            parts = await self.generate("status", 60, 3)
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(bot.byte_len(part) <= 60 for part in parts))
        self.assertTrue(parts[-1].endswith("…"))

    async def test_prompt_limit_is_what_the_parts_can_carry(self):
        client = FakeOllama(answer("ok"))
        with mock.patch.object(bot, "ollama_client", client):
            await self.generate("status", 100, 3)
        self.assertIn("at most 150 characters", client.requests[0]["messages"][0]["content"])

    async def test_empty_answer_gives_error_reply(self):
        self.assertEqual(await self.reply(FakeOllama(answer("   "))), bot.ERROR_REPLY)


class SendReplyTest(QuietTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(bot_meshcore, "PART_DELAY_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_sends_each_part(self):
        sent = []

        async def send(part):
            sent.append(part)
            return ok()

        await bot_meshcore.send_reply(send, ["one", "two", "three"])
        self.assertEqual(sent, ["one", "two", "three"])

    async def test_stops_on_send_error(self):
        sent = []

        async def send(part):
            sent.append(part)
            return error()

        await bot_meshcore.send_reply(send, ["one", "two", "three"])
        self.assertEqual(sent, ["one"])


class MapUploadTest(unittest.IsolatedAsyncioTestCase):
    SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")

    def setUp(self):
        import hashlib
        import struct

        digest = bytearray(hashlib.sha512(self.SEED).digest())
        digest[0] &= 248
        digest[31] = (digest[31] & 127) | 64
        self.private_key = bytes(digest)
        self.public_key = map_upload.ed25519_public_key(self.private_key)
        self.struct = struct
        self.uploads = []
        self.uploader = map_upload.MapUploader(
            {"radio_freq": 869.618, "radio_bw": 62.5, "radio_sf": 8, "radio_cr": 8}, self.private_key, self.uploads.append
        )

    def advert(self, node_type=2, timestamp=1790000000, name="Carmel RPT", tamper=False):
        app = bytes([0x80 | node_type]) + name.encode()
        stamp = self.struct.pack("<I", timestamp)
        signature = map_upload.ed25519_sign(self.private_key, self.public_key + stamp + app)
        if tamper:
            app = app[:-1] + b"X"
        return (bytes([0x11, 0x00]) + self.public_key + stamp + signature + app).hex()

    def test_matches_the_rfc_8032_vector(self):
        self.assertEqual(
            self.public_key.hex(), "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
        )
        signature = map_upload.ed25519_sign(self.private_key, b"")
        self.assertTrue(signature.hex().startswith("e5564300c360ac729086e2cc806e828a"))
        self.assertTrue(map_upload.ed25519_verify(self.public_key, b"", signature))
        self.assertFalse(map_upload.ed25519_verify(self.public_key, b"x", signature))

    async def test_uploads_a_signed_repeater_advert(self):
        self.assertEqual(await self.uploader.handle_packet(self.advert()), "Carmel RPT")
        body = json.loads(self.uploads[0])
        self.assertEqual(body["publicKey"], self.public_key.hex())
        data = json.loads(body["data"])
        self.assertEqual(data["params"], {"freq": 869.618, "cr": 8, "sf": 8, "bw": 62.5})
        self.assertEqual(data["links"], ["meshcore://" + self.advert()])
        import hashlib

        self.assertTrue(
            map_upload.ed25519_verify(
                self.public_key, hashlib.sha256(body["data"].encode()).digest(), bytes.fromhex(body["signature"])
            )
        )

    async def test_skips_companions_bad_signatures_and_other_packets(self):
        self.assertIsNone(await self.uploader.handle_packet(self.advert(node_type=1)))
        self.assertIsNone(await self.uploader.handle_packet(self.advert(tamper=True)))
        self.assertIsNone(await self.uploader.handle_packet("0a0000"))
        self.assertEqual(self.uploads, [])

    async def test_uploads_a_node_again_only_after_an_hour(self):
        await self.uploader.handle_packet(self.advert(timestamp=1790000000))
        self.assertIsNone(await self.uploader.handle_packet(self.advert(timestamp=1790000000)))
        self.assertIsNone(await self.uploader.handle_packet(self.advert(timestamp=1790003599)))
        self.assertEqual(await self.uploader.handle_packet(self.advert(timestamp=1790003600)), "Carmel RPT")
        self.assertEqual(len(self.uploads), 2)


class FakeMqttClient:
    def __init__(self):
        self.connected = True
        self.published = []

    def is_connected(self):
        return self.connected

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, json.loads(payload), retain))


class MqttObserverTest(unittest.TestCase):
    def setUp(self):
        import hashlib

        digest = bytearray(hashlib.sha512(MapUploadTest.SEED).digest())
        digest[0] &= 248
        digest[31] = (digest[31] & 127) | 64
        self.private_key = bytes(digest)
        self.public_key = map_upload.ed25519_public_key(self.private_key)
        self.client = FakeMqttClient()
        with mock.patch.object(mqtt_observer, "MQTT_IATA", "TLV"):
            self.observer = mqtt_observer.MqttObserver(
                FakeMeshCore().self_info, {"model": "Heltec V3", "ver": "1.14"}, self.private_key, self.client
            )

    def test_auth_token_is_a_jwt_signed_by_the_radio_key(self):
        import base64

        token = mqtt_observer.create_auth_token(self.private_key, "mqtt.meshmapper.net", now=1790000000)
        header, payload, signature = token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        self.assertEqual(claims["publicKey"], self.public_key.hex().upper())
        self.assertEqual(claims["aud"], "mqtt.meshmapper.net")
        self.assertEqual(claims["exp"] - claims["iat"], 3600)
        self.assertTrue(
            map_upload.ed25519_verify(self.public_key, f"{header}.{payload}".encode(), bytes.fromhex(signature))
        )

    def test_packet_message_matches_meshcoretomqtt(self):
        # Direct text message (type 2) over two one-byte hops
        raw = bytes([0x0A, 0x02, 0xBB, 0xAA]) + b"hello"
        message = mqtt_observer.packet_message({"payload": raw.hex(), "snr": 9.75, "rssi": -80}, "ClubBot", "ABCD")
        self.assertEqual(message["origin_id"], "ABCD")
        self.assertEqual(message["route"], "D")
        self.assertEqual(message["packet_type"], "2")
        self.assertEqual(message["len"], "9")
        self.assertEqual(message["payload_len"], "5")
        self.assertEqual(message["path"], "BB -> AA")
        self.assertEqual((message["SNR"], message["RSSI"]), ("9", "-80"))
        self.assertEqual(message["raw"], raw.hex().upper())
        import hashlib

        self.assertEqual(message["hash"], hashlib.sha256(b"\x02hello").digest()[:8].hex().upper())

    def test_flood_packets_have_no_path_and_garbage_is_skipped(self):
        message = mqtt_observer.packet_message({"payload": "1101bb" + "00" * 10}, "ClubBot", "ABCD")
        self.assertEqual(message["route"], "F")
        self.assertNotIn("path", message)
        self.assertIsNone(mqtt_observer.packet_message({"payload": "0a05bb"}, "ClubBot", "ABCD"))
        self.assertIsNone(mqtt_observer.packet_message({"payload": "zz"}, "ClubBot", "ABCD"))

    def test_publishes_packets_under_the_region_and_key(self):
        self.assertTrue(self.observer.handle_packet({"payload": "1101bb" + "00" * 10, "snr": 5, "rssi": -90}))
        topic, message, retain = self.client.published[0]
        self.assertEqual(topic, f"meshcore/TLV/{self.public_key.hex().upper()}/packets")
        self.assertEqual(message["origin"], "ClubBot")
        self.assertFalse(retain)
        self.client.connected = False
        self.assertFalse(self.observer.handle_packet({"payload": "1101bb" + "00" * 10}))

    def test_status_names_the_radio(self):
        status = self.observer.status_message("online")
        self.assertEqual(status["radio"], "869.618,62.5,8,8")
        self.assertEqual((status["model"], status["firmware_version"]), ("Heltec V3", "1.14"))


class StatusServerTest(QuietTestCase):
    async def asyncSetUp(self):
        self.mesh = FakeMeshCore()
        self.tools = MeshCoreTools(self.mesh, "test-model", asyncio.get_running_loop())
        self.tools.counters["replies_sent"] = 3
        self.tools.record("advert", "from <b>Carmel</b>")
        self.extra = {"repeaters": [{"repeater": "Carmel RPT", "battery mV": "4069"}], "map_upload": {}}
        telemetry = lambda: [{"name": "Carmel RPT", "points": [{"t": 1, "battery": 4069.0, "voltage": None, "temperature": 30.0}]}]
        self.server = status_server.StatusServer(
            self.tools, asyncio.get_running_loop(), lambda: self.extra, telemetry, "ClubBot"
        )
        self.client = self.server.app.test_client()

    async def get(self, path):
        # The page waits on the event loop for the radio, so it is fetched from a worker thread as Flask would
        return await asyncio.to_thread(self.client.get, path)

    async def text(self, path):
        response = await self.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.get_data(as_text=True)

    async def test_api_reports_health_events_and_extras(self):
        body = (await self.get("/api/status")).get_json()
        self.assertTrue(body["health"]["radio_connected"])
        self.assertEqual(body["health"]["counters"], {"replies_sent": 3})
        self.assertEqual(body["health"]["firmware"]["model"], "Heltec V3")
        self.assertIn("advert from <b>Carmel</b>", body["events"][0])
        self.assertEqual(body["extra"]["repeaters"][0]["battery mV"], "4069")

    async def test_each_section_is_its_own_tab(self):
        page = await self.text("/")
        for tab in ("/bot", "/radio", "/repeaters", "/events", "/graphs"):
            self.assertIn(f'href="{tab}"', page)
        # Empty extras get no tab
        self.assertNotIn('href="/map_upload"', page)
        self.assertEqual((await self.get("/map_upload")).status_code, 404)
        self.assertIn("replies sent", page)
        self.assertIn("Heltec V3", await self.text("/radio"))
        self.assertIn("Carmel RPT", await self.text("/repeaters"))
        self.assertIn('aria-current="page">Events', await self.text("/events"))

    async def test_contacts_tab_lists_heard_contacts(self):
        self.extra["contacts"] = self.tools.heard_contacts()
        self.assertEqual(self.extra["contacts"], [{"name": "4X1ABC", "type": "companion", "hops": 2, "heard": "5m ago"}])
        page = await self.text("/contacts")
        self.assertIn("4X1ABC", page)
        # Never heard from, so not listed
        self.assertNotIn("Carmel RPT", page)

    async def test_events_tab_does_not_ask_the_radio(self):
        async def broken():
            raise AssertionError("radio queried")

        self.tools.bot_health = broken
        self.assertIn("advert from", await self.text("/events"))
        self.assertIn("Carmel RPT", await self.text("/repeaters"))

    async def test_page_escapes_names_heard_on_the_mesh(self):
        page = await self.text("/events")
        self.assertIn("&lt;b&gt;Carmel&lt;/b&gt;", page)
        self.assertNotIn("<b>Carmel</b>", page)

    async def test_shows_the_bot_when_the_radio_fails(self):
        async def broken():
            raise ConnectionError("radio gone")

        self.tools.bot_health = broken
        body = (await self.get("/api/status")).get_json()
        self.assertEqual(body["health"]["counters"], {"replies_sent": 3})
        self.assertIn("radio gone", body["health"]["error"])
        self.assertIn("radio gone", await self.text("/"))
        self.assertIn("Could not read the radio", await self.text("/radio"))

    async def test_graphs_page_loads_the_telemetry(self):
        page = await self.text("/graphs")
        self.assertIn('data-source="/api/telemetry"', page)
        self.assertIn("/static/graphs.js", page)
        self.assertEqual((await self.get("/static/graphs.js")).status_code, 200)
        body = (await self.get("/api/telemetry")).get_json()
        self.assertEqual(body[0]["points"][0]["battery"], 4069.0)

    async def test_no_graphs_tab_without_telemetry(self):
        server = status_server.StatusServer(self.tools, asyncio.get_running_loop())
        client = server.app.test_client()
        self.assertNotIn('href="/graphs"', (await asyncio.to_thread(client.get, "/events")).get_data(as_text=True))
        self.assertEqual((await asyncio.to_thread(client.get, "/api/telemetry")).status_code, 404)

    async def test_serves_over_http_and_stops(self):
        import urllib.request

        self.server.start("127.0.0.1", 0)
        url = f"http://127.0.0.1:{self.server.server.server_port}/api/status"
        body = await asyncio.to_thread(lambda: json.loads(urllib.request.urlopen(url, timeout=5).read()))
        self.assertEqual(body["health"]["model"], "test-model")
        await asyncio.to_thread(self.server.stop)
        self.assertFalse(self.server.thread.is_alive())


class LatestRowsTest(unittest.TestCase):
    def test_keeps_the_newest_probe_of_each_repeater(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "probe.csv")
            repeater_telemetry.write_probe_row(path, {"time": "t1", "repeater": "B", "status": {"bat": 4000}})
            repeater_telemetry.write_probe_row(path, {"time": "t2", "repeater": "A", "error": "no answer"})
            repeater_telemetry.write_probe_row(path, {"time": "t3", "repeater": "B", "status": {"bat": 4100}})
            rows = repeater_telemetry.latest_rows(path)
        self.assertEqual([(r["repeater"], r["probed"]) for r in rows], [("A", "t2"), ("B", "t3")])
        self.assertEqual(rows[1]["battery mV"], "4100")
        self.assertEqual(rows[0]["error"], "no answer")
        self.assertEqual(repeater_telemetry.latest_rows(os.path.join(folder, "missing.csv")), [])

    def test_history_groups_readings_by_repeater(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "probe.csv")
            repeater_telemetry.write_probe_row(path, {"time": "2026-10-28T10:00:00", "repeater": "B", "status": {"bat": 4000}})
            repeater_telemetry.write_probe_row(path, {"time": "2026-10-28T09:00:00", "repeater": "B", "status": {"bat": 3990}})
            repeater_telemetry.write_probe_row(path, {"time": "2026-10-28T10:00:00", "repeater": "A", "error": "no answer"})
            repeater_telemetry.write_probe_row(
                path,
                {"time": "2026-10-28T11:00:00", "repeater": "C", "telemetry": [{"channel": 1, "type": "temperature", "value": 31.5}]},
            )
            series = repeater_telemetry.history(path)
        self.assertEqual([s["name"] for s in series], ["B", "C"])
        self.assertEqual([p["battery"] for p in series[0]["points"]], [3990.0, 4000.0])
        self.assertLess(series[0]["points"][0]["t"], series[0]["points"][1]["t"])
        self.assertEqual(series[1]["points"][0], {**series[1]["points"][0], "battery": None, "temperature": 31.5})


class BotEndToEndTest(QuietTestCase):
    """Runs main() itself with the radio and the model replaced by fakes."""

    async def asyncSetUp(self):
        self.mesh = FakeMeshCore()
        self.model = FakeOllama()

        async def create_serial(port, debug=False):
            return self.mesh

        for patcher in (
            mock.patch.object(bot_meshcore, "MESHCORE_PORT", "/dev/fake"),
            mock.patch.object(bot_meshcore, "MESHCORE_BLE", ""),
            mock.patch.object(bot_meshcore, "MESHCORE_MAP_UPLOAD", False),
            mock.patch.object(bot_meshcore, "MESHCORE_MQTT_OBSERVER", False),
            mock.patch.object(bot_meshcore, "STATUS_PORT", 0),
            mock.patch.object(bot_meshcore, "MESHCORE_CHANNELS", ["bot-il"]),
            mock.patch.object(bot_meshcore, "PART_DELAY_SECONDS", 0),
            mock.patch.object(bot_meshcore, "probe_forever", lambda meshcore: asyncio.sleep(3600)),
            mock.patch.object(bot_meshcore.MeshCore, "create_serial", create_serial),
            mock.patch.object(bot, "ollama_client", self.model),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        self.bot = asyncio.create_task(bot_meshcore.main())
        await self.wait_for(lambda: EventType.CHANNEL_MSG_RECV in self.mesh.subscribers)

    async def asyncTearDown(self):
        self.bot.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.bot
        self.assertTrue(self.mesh.disconnected)

    async def wait_for(self, condition):
        for _ in range(200):
            if condition():
                return
            await asyncio.sleep(0.005)
        self.fail("timed out waiting for the bot")

    async def idle(self):
        for _ in range(20):
            await asyncio.sleep(0)

    async def test_answers_on_bot_channel_with_a_mention_prefix(self):
        self.model.script = [tool_request("link_test"), answer("received, 2 hops")]
        await self.mesh.emit(EventType.CHANNEL_MSG_RECV, {"channel_idx": 1, "text": "4X1ABC: test", "path_len": 2})
        await self.wait_for(lambda: self.mesh.commands.channel_messages)
        self.assertEqual(self.mesh.commands.channel_messages, [(1, "@[4X1ABC] received, 2 hops")])
        self.assertIn("4X1ABC on bot-il", self.model.requests[0]["messages"][0]["content"])
        self.assertIn('"path_len":2', self.model.requests[1]["messages"][-1]["content"])

    async def test_advertises_once_on_startup_without_flooding(self):
        self.assertEqual(self.mesh.commands.adverts, [False])

    async def test_ignores_public_channel_without_a_mention(self):
        await self.mesh.emit(EventType.CHANNEL_MSG_RECV, {"channel_idx": 0, "text": "4X1ABC: hello all"})
        await self.idle()
        self.assertEqual(self.model.requests, [])
        self.assertEqual(self.mesh.commands.channel_messages, [])

    async def test_ignores_other_user_mentions_on_monitored_channel(self):
        await self.mesh.emit(EventType.CHANNEL_MSG_RECV, {"channel_idx": 1, "text": "4X1ABC: @[Other User] hello"})
        await self.idle()
        self.assertEqual(self.model.requests, [])
        self.assertEqual(self.mesh.commands.channel_messages, [])

    async def test_answers_a_mention_on_a_channel_that_is_not_listed(self):
        self.model.script = [answer("hi")]
        await self.mesh.emit(EventType.CHANNEL_MSG_RECV, {"channel_idx": 0, "text": "4X1ABC: @bot hello"})
        await self.wait_for(lambda: self.mesh.commands.channel_messages)
        self.assertEqual(self.mesh.commands.channel_messages, [(0, "@[4X1ABC] hi")])

    async def test_ignores_its_own_channel_messages(self):
        await self.mesh.emit(EventType.CHANNEL_MSG_RECV, {"channel_idx": 1, "text": "ClubBot: hello"})
        await self.idle()
        self.assertEqual(self.model.requests, [])

    async def test_answers_a_direct_message(self):
        self.model.script = [answer("shalom")]
        await self.mesh.emit(EventType.CONTACT_MSG_RECV, {"pubkey_prefix": KEY_A[:12], "text": "hi", "txt_type": 0})
        await self.wait_for(lambda: self.mesh.commands.direct_messages)
        destination, text = self.mesh.commands.direct_messages[0]
        self.assertEqual(destination["adv_name"], "4X1ABC")
        self.assertEqual(text, "shalom")
        self.assertIn("4X1ABC on direct", self.model.requests[0]["messages"][0]["content"])

    async def test_ignores_non_text_direct_messages(self):
        await self.mesh.emit(EventType.CONTACT_MSG_RECV, {"pubkey_prefix": KEY_A[:12], "text": "cli", "txt_type": 1})
        await self.idle()
        self.assertEqual(self.model.requests, [])

    async def test_network_events_and_counters_reach_the_model(self):
        await self.mesh.emit(EventType.ADVERTISEMENT, {"public_key": KEY_A})
        await self.mesh.emit(EventType.PATH_UPDATE, {"public_key": KEY_B})
        await self.mesh.emit(EventType.NEW_CONTACT, {"adv_name": "4Z5NEW"})
        await self.mesh.emit(EventType.DISCONNECTED, {"reason": "serial lost"})
        await self.mesh.emit(EventType.CONTACT_MSG_RECV, {"pubkey_prefix": KEY_A[:12], "text": "hi", "txt_type": 0})
        self.model.script = [answer("first")]
        await self.wait_for(lambda: self.mesh.commands.direct_messages)

        self.model.script = [tool_request("recent_network_events"), tool_request("bot_health"), answer("ok")]
        await self.mesh.emit(EventType.CHANNEL_MSG_RECV, {"channel_idx": 1, "text": "4X1ABC: events and status?"})
        await self.wait_for(lambda: self.mesh.commands.channel_messages)

        events = self.model.requests[-2]["messages"][-1]["content"]
        for expected in (
            "advert from 4X1ABC",
            "path update for Carmel RPT",
            "new contact 4Z5NEW",
            "radio disconnected serial lost",
            "channel message from 4X1ABC on bot-il",
        ):
            self.assertIn(expected, events)
        # Who messaged the bot privately is not shared
        self.assertIn("direct message\"", events)
        self.assertNotIn("direct message from", events)

        health = self.model.requests[-1]["messages"][-1]["content"]
        self.assertIn('"messages_received":2', health)
        self.assertIn('"replies_sent":1', health)

    async def test_model_failure_sends_the_error_reply(self):
        self.model.script = [RuntimeError("down")]
        await self.mesh.emit(EventType.CONTACT_MSG_RECV, {"pubkey_prefix": KEY_A[:12], "text": "hi", "txt_type": 0})
        await self.wait_for(lambda: self.mesh.commands.direct_messages)
        self.assertEqual(self.mesh.commands.direct_messages[0][1], bot.ERROR_REPLY)


BOT_NUM = 0xE9D66B4E
USER_NUM = 0x0A0B0C0D


class FakeMeshtastic:
    def __init__(self):
        primary = channel_pb2.Channel(index=0, role=channel_pb2.Channel.Role.PRIMARY)
        primary.settings.name = "IL-Club"
        primary.settings.psk = b"secret"
        lora = config_pb2.Config.LoRaConfig(region="EU_868", use_preset=True, hop_limit=3)
        self.myInfo = SimpleNamespace(my_node_num=BOT_NUM)
        self.localNode = SimpleNamespace(
            channels=[primary, channel_pb2.Channel(index=1)], localConfig=SimpleNamespace(lora=lora)
        )
        self.sent = []
        # The radio's node database, which also holds the radio itself
        self.nodes = {"!e9d66b4e": {**self.getMyNodeInfo(), "lastHeard": int(time.time())}}

    def getMyNodeInfo(self):
        return {
            "num": BOT_NUM,
            "user": {"id": "!e9d66b4e", "longName": "ClubBot", "shortName": "BOT", "hwModel": "HELTEC_V3"},
            "deviceMetrics": {"batteryLevel": 88, "channelUtilization": 4.2},
        }

    def add_node(self, num, long_name, short_name, user=None, **node):
        """Put a node in the radio's database, the way the radio does when it hears one."""
        user = {"id": f"!{num:08x}", "longName": long_name, "shortName": short_name, **(user or {})}
        self.nodes[f"!{num:08x}"] = {"num": num, "user": user, **node}

    def sendText(self, text, destinationId=None, channelIndex=0):
        self.sent.append((destinationId, channelIndex, text))


def node_info_packet(num, long_name, short_name, **user):
    user = {"longName": long_name, "shortName": short_name, "raw": object(), **user}
    return {"from": num, "decoded": {"portnum": "NODEINFO_APP", "user": user}}


def text_packet(text, to=0xFFFFFFFF, **extra):
    return {"from": USER_NUM, "to": to, "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": text}, **extra}


class MeshtasticToolsTest(QuietTestCase):
    def setUp(self):
        super().setUp()
        self.radio = FakeMeshtastic()
        self.tools = MeshtasticTools(self.radio, "test-model")
        self.radio.add_node(
            USER_NUM, "4X1ABC", "ABC", {"hwModel": "TBEAM", "isLicensed": True},
            lastHeard=int(time.time()) - 300, hopsAway=2, position={"latitude": 32.08, "longitude": 34.78},
        )
        self.radio.add_node(2, "Carmel RPT", "CRM", {"role": "ROUTER_LATE"}, lastHeard=int(time.time()) - 60)
        self.radio.add_node(3, "Weather", "WX", {"role": "SENSOR"})

    def test_every_advertised_tool_is_implemented(self):
        self.assertEqual([s["function"]["name"] for s in self.tools.specs], [s["function"]["name"] for s in tools.TOOL_SPECS])
        for spec in self.tools.specs:
            self.assertTrue(callable(getattr(self.tools, spec["function"]["name"])))

    def test_descriptions_do_not_promise_meshcore_fields(self):
        described = json.dumps(self.tools.specs, ensure_ascii=False)
        self.assertNotIn("path_len", described)
        self.assertNotIn("room", described)
        self.assertIn("path_len", json.dumps(tools.TOOL_SPECS))

    def test_list_contacts_reports_contact_types(self):
        result = self.tools.list_contacts()
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["by_type"], {"companion": 2, "repeater": 1, "sensor": 1})
        user = next(c for c in result["contacts"] if c["name"] == "4X1ABC")
        self.assertEqual(user, {
            "name": "4X1ABC", "short": "ABC", "id": "!0a0b0c0d", "type": "companion", "role": "client",
            "hops": 2, "heard": "5m ago", "hardware": "TBEAM", "licensed_ham": True, "location": [32.08, 34.78],
        })
        # Most recently heard first; the radio's database holds the radio itself too
        self.assertEqual([c["name"] for c in result["contacts"]], ["ClubBot", "Carmel RPT", "4X1ABC", "Weather"])
        self.assertEqual(result["contacts"][3]["heard"], "never")
        self.assertEqual(result["contacts"][3]["hops"], "unknown")

    def test_list_contacts_reads_the_radio_each_time(self):
        self.radio.add_node(5, "Late", "LT")
        self.assertEqual(self.tools.list_contacts()["total"], 5)
        # A node the radio has heard but that has not announced itself yet
        self.radio.nodes["!00000006"] = {"num": 6}
        self.assertEqual(self.tools.list_contacts(query="!00000006")["contacts"][0]["name"], "!00000006")

    def test_list_contacts_filters(self):
        self.assertEqual([c["name"] for c in self.tools.list_contacts(kind="Repeater")["contacts"]], ["Carmel RPT"])
        self.assertEqual([c["name"] for c in self.tools.list_contacts(kind="router_late")["contacts"]], ["Carmel RPT"])
        self.assertEqual(self.tools.list_contacts(kind="room")["matching"], 0)
        self.assertEqual(self.tools.list_contacts(query="!0a0b")["contacts"][0]["name"], "4X1ABC")
        self.assertEqual(self.tools.list_contacts(query=None, kind=None)["matching"], 4)

    def test_old_router_client_role_is_a_repeater(self):
        self.radio.add_node(4, "Old", "OLD", {"role": "ROUTER_CLIENT"})
        self.assertEqual(self.tools.list_contacts(query="old")["contacts"][0]["type"], "repeater")

    def test_link_test_reports_hops_and_signal(self):
        details = self.tools.link_test(text_packet("ping", hopStart=3, hopLimit=1, rxSnr=6.5, rxRssi=-90))
        self.assertEqual(details["sender"], "4X1ABC")
        self.assertEqual(details["hops"], 2)
        self.assertEqual(details["snr_db"], 6.5)
        self.assertEqual(details["rssi_dbm"], -90)
        self.assertEqual(self.tools.link_test(text_packet("ping", hopStart=3, hopLimit=3))["route"], "heard directly, no relays")
        self.assertEqual(self.tools.link_test(text_packet("ping"))["hops"], "unknown")

    def test_list_channels_skips_disabled_slots_and_hides_keys(self):
        self.assertEqual(self.tools.list_channels(), [{"index": 0, "name": "IL-Club", "role": "primary"}])
        self.assertNotIn("secret", self.tools.call("list_channels", {}, {}))

    def test_bot_health(self):
        self.tools.counters["replies_sent"] += 3
        health = self.tools.bot_health()
        self.assertEqual(health["bot_uptime"], "0s")
        self.assertEqual(health["contacts"], 4)
        self.assertEqual(health["counters"], {"replies_sent": 3})
        self.assertEqual(health["radio"]["name"], "ClubBot")
        self.assertEqual(health["device"]["batteryLevel"], 88)
        self.assertEqual(health["lora"]["region"], "EU_868")
        json.loads(self.tools.call("bot_health", {}, {}))

    def test_call_handles_bad_requests(self):
        self.assertEqual(self.tools.call("nope", {}, {}), "Unknown tool: nope")
        self.assertTrue(self.tools.call("list_channels", {"extra": 1}, {}).startswith("Tool error:"))


class MeshtasticEndToEndTest(QuietTestCase):
    """Feeds packets to the receive callback with the radio and the model replaced by fakes."""

    def setUp(self):
        super().setUp()
        self.radio = FakeMeshtastic()
        self.model = FakeOllama()
        for patcher in (
            mock.patch.object(bot_meshtastic, "tools", MeshtasticTools(self.radio, "test-model"), create=True),
            mock.patch.object(bot_meshtastic, "PART_DELAY_SECONDS", 0),
            mock.patch.object(bot, "ollama_client", self.model),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tools = bot_meshtastic.tools
        self.radio.add_node(USER_NUM, "4X1ABC", "ABC")

    def receive(self, packet):
        bot_meshtastic.on_receive_packet(packet, self.radio)

    def test_answers_a_mention_on_the_channel_with_a_mention(self):
        self.model.script = [tool_request("link_test"), answer("received, 2 hops")]
        self.receive(text_packet("@bot test", hopStart=3, hopLimit=1))
        self.assertEqual(self.radio.sent, [(None, 0, "@!0a0b0c0d received, 2 hops")])
        self.assertIn("4X1ABC on IL-Club", self.model.requests[0]["messages"][0]["content"])
        self.assertIn('"hops":2', self.model.requests[1]["messages"][-1]["content"])

    def test_answers_a_mention_by_node_id(self):
        self.model.script = [answer("hi")]
        self.receive(text_packet("@!e9d66b4e hello"))
        self.assertEqual(self.radio.sent, [(None, 0, "@!0a0b0c0d hi")])

    def test_ignores_the_channel_without_a_mention(self):
        self.receive(text_packet("hello all"))
        self.assertEqual(self.model.requests, [])
        self.assertEqual(self.radio.sent, [])

    def test_answers_a_direct_message(self):
        self.model.script = [answer("shalom")]
        self.receive(text_packet("hi", to=BOT_NUM))
        self.assertEqual(self.radio.sent, [(USER_NUM, 0, "shalom")])
        self.assertIn("4X1ABC on direct", self.model.requests[0]["messages"][0]["content"])

    def test_channel_parts_fit_a_message_with_the_mention(self):
        self.model.script = [answer("מילה " * 200)]
        self.receive(text_packet("@bot status"))
        self.assertEqual(len(self.radio.sent), bot_meshtastic.MAX_REPLY_PARTS)
        for _, _, text in self.radio.sent:
            self.assertTrue(text.startswith("@!0a0b0c0d "))
            self.assertLessEqual(bot.byte_len(text), bot_meshtastic.MAX_MESSAGE_BYTES)

    def test_model_failure_sends_the_error_reply(self):
        self.model.script = [RuntimeError("down")]
        self.receive(text_packet("hi", to=BOT_NUM))
        self.assertEqual(self.radio.sent, [(USER_NUM, 0, bot.ERROR_REPLY)])
        self.assertEqual(self.tools.counters["model_errors"], 1)

    def test_network_events_and_counters_reach_the_model(self):
        self.receive(node_info_packet(2, "Carmel RPT", "CRM", role="ROUTER"))
        self.receive(node_info_packet(USER_NUM, "4X1ABC", "ABC"))
        self.receive({"from": 2, "decoded": {"portnum": "TELEMETRY_APP", "telemetry": {"deviceMetrics": {"voltage": 4.0}}}})
        self.model.script = [answer("first")]
        self.receive(text_packet("hi", to=BOT_NUM))
        self.model.script = [tool_request("recent_network_events"), tool_request("bot_health"), answer("ok")]
        self.receive(text_packet("@bot events and status?"))

        events = self.model.requests[-2]["messages"][-1]["content"]
        for expected in (
            "new node from Carmel RPT",
            "node info from 4X1ABC",
            "telemetry from Carmel RPT",
            "channel message from 4X1ABC on IL-Club",
        ):
            self.assertIn(expected, events)
        # Who messaged the bot privately is not shared
        self.assertIn("direct message\"", events)
        self.assertNotIn("direct message from", events)

        health = self.model.requests[-1]["messages"][-1]["content"]
        self.assertIn('"messages_received":2', health)
        self.assertIn('"replies_sent":1', health)


class MeshtasticConnectTest(QuietTestCase):
    def test_uses_serial_unless_a_bluetooth_device_is_set(self):
        with mock.patch.object(bot_meshtastic, "MESHTASTIC_BLE", ""), mock.patch.object(
            bot_meshtastic, "MESHTASTIC_PORT", "/dev/fake"
        ), mock.patch("meshtastic.serial_interface.SerialInterface") as serial:
            self.assertIs(bot_meshtastic.connect(), serial.return_value)
        serial.assert_called_once_with(devPath="/dev/fake")

    def test_uses_bluetooth_when_a_device_is_set(self):
        with mock.patch.object(bot_meshtastic, "MESHTASTIC_BLE", "Meshtastic_6b4e"), mock.patch(
            "meshtastic.ble_interface.BLEInterface"
        ) as ble, mock.patch("meshtastic.serial_interface.SerialInterface") as serial:
            self.assertIs(bot_meshtastic.connect(), ble.return_value)
        ble.assert_called_once_with("Meshtastic_6b4e")
        serial.assert_not_called()


class StartupTest(QuietTestCase):
    async def test_main_needs_a_port_or_a_bluetooth_device(self):
        with mock.patch.object(bot_meshcore, "MESHCORE_PORT", ""), mock.patch.object(bot_meshcore, "MESHCORE_BLE", ""):
            self.assertEqual(await bot_meshcore.main(), 1)

    async def test_main_reports_a_failed_connection(self):
        async def create_serial(port, debug=False):
            return None

        with mock.patch.object(bot_meshcore, "MESHCORE_PORT", "/dev/fake"), mock.patch.object(
            bot_meshcore, "MESHCORE_BLE", ""
        ), mock.patch.object(bot_meshcore.MeshCore, "create_serial", create_serial):
            self.assertEqual(await bot_meshcore.main(), 1)

    async def test_main_reports_a_bluetooth_device_that_is_not_found(self):
        async def create_ble(address, debug=False):
            raise ConnectionError("Failed to connect to device")

        with mock.patch.object(bot_meshcore, "MESHCORE_BLE", "MeshCore-Bot"), mock.patch.object(
            bot_meshcore.MeshCore, "create_ble", create_ble
        ):
            self.assertEqual(await bot_meshcore.main(), 1)


class MeshCoreConnectTest(QuietTestCase):
    async def test_uses_serial_unless_a_bluetooth_device_is_set(self):
        with mock.patch.object(bot_meshcore, "MESHCORE_BLE", ""), mock.patch.object(
            bot_meshcore, "MESHCORE_PORT", "/dev/fake"
        ), mock.patch.object(bot_meshcore.MeshCore, "create_serial") as serial:
            self.assertIs(await bot_meshcore.connect(), serial.return_value)
        serial.assert_awaited_once_with("/dev/fake", debug=bot_meshcore.MESHCORE_DEBUG)

    async def test_uses_bluetooth_when_a_device_is_set(self):
        with mock.patch.object(bot_meshcore, "MESHCORE_BLE", "MeshCore-Bot"), mock.patch.object(
            bot_meshcore.MeshCore, "create_ble"
        ) as ble, mock.patch.object(bot_meshcore.MeshCore, "create_serial") as serial:
            self.assertIs(await bot_meshcore.connect(), ble.return_value)
        ble.assert_awaited_once_with("MeshCore-Bot", debug=bot_meshcore.MESHCORE_DEBUG)
        serial.assert_not_called()


if __name__ == "__main__":
    unittest.main()
