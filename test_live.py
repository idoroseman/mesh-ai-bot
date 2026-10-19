"""Live tests: the real model on the Ollama server, with a fake radio.

These check what the fake-model tests cannot: that the model picks the right tool
and writes a usable reply. They are skipped when the server cannot be reached.

Run with: .venv/bin/python -m unittest -v test_live
The server and model come from config.json.
"""

import asyncio
import unittest
from unittest import mock

import ollama

import bot
from test_bot import METADATA, FakeMeshCore
from tools_meshcore import MeshCoreTools
from tools_meshtastic import MeshtasticTools

# The model is told to stay within MAX_BYTES * MAX_PARTS // 2 characters
MAX_BYTES = 100
MAX_PARTS = 4
MAX_CHARS = MAX_BYTES * MAX_PARTS // 2

# (what the user sends, the tool the model is expected to call)
TOOL_CASES = [
    ("ping", "ping"),
    ("test", "link_test"),
    ("בדיקה", "link_test"),
    ("אילו ממסרים יש ברשת?", "list_contacts"),
    ("Tell me about the contact 4X1ABC", "list_contacts"),
    ("Which channels do you have?", "list_channels"),
    ("What happened on the mesh network recently?", "recent_network_events"),
    ("מה מצב הבוט?", "bot_health"),
]


class RecordingTools(MeshCoreTools):
    """MeshCoreTools that remembers which tools the model called."""

    def __init__(self, *args):
        super().__init__(*args)
        self.called = []

    def call(self, name, arguments, metadata):
        self.called.append(name)
        return super().call(name, arguments, metadata)


class LiveModelTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        try:
            models = [m.model for m in ollama.Client(host=bot.OLLAMA_HOST, timeout=5).list().models]
        except Exception as exc:
            raise unittest.SkipTest(f"Ollama is not reachable at {bot.OLLAMA_HOST}: {exc}")
        if bot.MODEL_NAME not in models:
            raise unittest.SkipTest(f"{bot.MODEL_NAME} is not on {bot.OLLAMA_HOST}; it has: {', '.join(models)}")

    async def asyncSetUp(self):
        patcher = mock.patch.object(bot, "ollama_client", ollama.Client(host=bot.OLLAMA_HOST, timeout=180))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tools = RecordingTools(FakeMeshCore(), bot.MODEL_NAME, asyncio.get_running_loop())
        self.tools.record("advert", "from 4X1ABC")
        self.tools.record("new contact", "4Z5NEW")

    async def ask(self, text):
        self.tools.called.clear()
        metadata = {**METADATA, "text": text}
        parts = await asyncio.to_thread(bot.generate_bot_reply, text, metadata, MAX_BYTES, MAX_PARTS, self.tools)
        reply = " ".join(parts)
        print(f"\n  Q: {text}\n  tools: {self.tools.called}\n  A ({len(reply)} chars): {reply}")
        return reply

    async def test_model_supports_tool_calling(self):
        await self.ask("ping")
        self.assertTrue(self.tools.model_supports_tools, f"{bot.MODEL_NAME} does not support tools")

    async def test_model_picks_the_right_tool(self):
        for text, expected in TOOL_CASES:
            with self.subTest(text=text):
                reply = await self.ask(text)
                self.assertIn(expected, self.tools.called)
                self.assertNotEqual(reply, bot.ERROR_REPLY)

    async def test_general_question_gets_an_answer(self):
        reply = await self.ask("What is SWR?")
        self.assertNotEqual(reply, bot.ERROR_REPLY)

    async def test_replies_fit_the_mesh_limit(self):
        for text in ("ping", "Explain how to build a dipole antenna for 40 meters", "מה מצב הבוט?"):
            with self.subTest(text=text):
                reply = await self.ask(text)
                self.assertLessEqual(len(reply), MAX_CHARS, "the model ignored the length limit")
                self.assertFalse(reply.endswith("…"), "the reply was too long and got cut")
                self.assertNotRegex(reply, r"[*#`]", "the model used markdown")

    async def test_answers_use_the_background_and_tool_data(self):
        # The field day is in the Meshtastic club's background
        with mock.patch.object(self.tools, "background_path", MeshtasticTools.background_path):
            self.assertRegex(await self.ask("מתי יום השדה הבא?"), r"10[/.]10|10 ב?אוקטובר|October 10")
        self.assertIn("bot-il", await self.ask("Which channels do you have?"))


if __name__ == "__main__":
    unittest.main()
