import re
import time
from typing import Any, Dict, List

import ollama

from config import CONFIG

OLLAMA_HOST = CONFIG["ollama_host"]
MODEL_NAME = CONFIG["model_name"]
MAX_TOOL_ROUNDS = CONFIG["max_tool_rounds"]

BOT_LANGUAGE = CONFIG["bot_language"]
BOT_MENTIONS = tuple(CONFIG["bot_mentions"])
ERROR_REPLY = CONFIG["error_reply"]

ollama_client = ollama.Client(host=OLLAMA_HOST)


def build_system_prompt(metadata: Dict[str, Any], max_chars: int, background: str = "") -> str:
    """The instructions every network shares, around the background and events of the community this network serves."""
    return (
        f"You are a terse {BOT_LANGUAGE}-speaking assistant on a LoRa mesh radio network.\n"
        f"{background}\n"
        "\n## Language\n"
        f"Prefer {BOT_LANGUAGE} in conversation, but if the user writes in another language, "
        f"reply in that language unless they ask specifically for {BOT_LANGUAGE}. "
        "Keep names, callsigns, URLs and technical terms (SNR, RSSI, hops, SF, LoRa) in their original Latin form.\n"
        "\n## Reply format\n"
        "Your reply is sent over a low-bandwidth LoRa mesh network. "
        f"Reply in plain text only, no markdown, no emojis, at most {max_chars} characters. "
        "Airtime is scarce, so be brief: answer only what was asked, in one or two short sentences, "
        "and use fewer words whenever fewer will do. Do not greet the user, repeat their question, "
        "add encouragement or small talk, offer more help, or end with a question. "
        "Put the answer first and drop details the user did not ask for. "
        "A link is worth more than an explanation: give the URL from the background when it answers the question.\n"
        "\n## Context"
        f"\nCurrent local time: {time.strftime('%Y-%m-%d %H:%M')}."
        f"\nYou are talking to {metadata.get('user') or 'an unknown user'} on {metadata.get('channel') or 'direct'}."
        " Do not address the user by name; the reply is already sent to them.\n"
        "\n## Tools and live data\n"
        "Use a tool whenever the answer depends on the live state of the mesh or of the bot, and never answer such "
        "questions from memory. Choose the tool that fits:\n"
        "- 'ping': answer only 'pong'.\n"
        "- 'test' or 'בדיקה' (checks that the bot hears the user): one short line confirming the message was "
        "received, with the hops and signal figures from the link test.\n"
        "- Which nodes, repeaters, rooms or sensors exist, how many, or one contact's details: the contact list.\n"
        "- What happened recently on the mesh: the recent network events.\n"
        "- The channels, or the bot's own health and radio status: the matching tool.\n"
        "Summarize only the most relevant part of a tool result so it fits the length limit, and give ages and "
        "counts as plain numbers. If a tool fails or returns nothing, say so in a few words instead of guessing. "
        "Community news and events are in the background above.\n"
        "\n## Accuracy\n"
        "Never invent values, names, dates, links or coordinates that are not in the background or the tool results. "
        "If you do not know a specific local fact, say so in a few words and suggest how to verify it with the "
        "community or official sources. For general radio and mesh questions you may answer from your own "
        "knowledge, but say when you are unsure. Politely decline requests that are unrelated to the mesh, "
        "radio and the community in one short sentence."
    )


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def byte_len(value: str) -> int:
    return len(value.encode("utf-8"))


def split_for_mesh(text: str, max_bytes: int, max_parts: int) -> List[str]:
    """Split text on word boundaries into parts that each fit in one mesh message."""
    parts: List[str] = []
    current = ""
    for word in normalize_text(text).split(" "):
        candidate = f"{current} {word}" if current else word
        if byte_len(candidate) <= max_bytes:
            current = candidate
            continue
        if current:
            parts.append(current)
        while byte_len(word) > max_bytes:
            head = word.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
            parts.append(head)
            word = word[len(head):]
        current = word
    if current:
        parts.append(current)

    if len(parts) > max_parts:
        parts = parts[:max_parts]
        last = parts[-1].encode("utf-8")[: max_bytes - byte_len("…")].decode("utf-8", "ignore")
        parts[-1] = last.rstrip() + "…"
    return parts


def chat_with_tools(messages: List[Any], tools: Any, metadata: Dict[str, Any]) -> str:
    """Let the model look up live mesh data through tool calls before it writes the reply."""
    if tools.model_supports_tools:
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                response = ollama_client.chat(model=MODEL_NAME, messages=messages, tools=tools.specs)
                if not response.message.tool_calls:
                    return response.message.content or ""
                messages.append(response.message)
                for tool_call in response.message.tool_calls:
                    name, arguments = tool_call.function.name, dict(tool_call.function.arguments or {})
                    result = tools.call(name, arguments, metadata)
                    print(f"Tool {name}({arguments}) -> {result[:200]}")
                    messages.append({"role": "tool", "tool_name": name, "content": result})
        except ollama.ResponseError as exc:
            if "does not support tools" not in str(exc):
                raise
            print(f"{MODEL_NAME} does not support tools, sending mesh data in the prompt instead")
            tools.model_supports_tools = False

    if not tools.model_supports_tools:
        # Without tool calling, hand the model a snapshot of everything it could have looked up
        names = [spec["function"]["name"] for spec in tools.specs]
        snapshot = [f"{name}: {tools.call(name, {}, metadata)}" for name in names]
        messages[0]["content"] += "\n\nLive mesh data:\n" + "\n".join(snapshot)
    response = ollama_client.chat(model=MODEL_NAME, messages=messages)
    return response.message.content or ""


def generate_bot_reply(
    text: str, metadata: Dict[str, Any], max_bytes: int, max_reply_parts: int, tools: Any
) -> List[str]:
    """Answer one user message; the reply comes back as parts that each fit in one mesh message."""
    # Hebrew takes 2 bytes per character
    max_chars = max_bytes * max_reply_parts // 2
    messages = [
        {"role": "system", "content": build_system_prompt(metadata, max_chars, tools.background())},
        {"role": "user", "content": text[:4000]},
    ]
    try:
        answer = chat_with_tools(messages, tools, metadata)
        # The model sometimes leaks a control token such as '<channel|>' into its answer
        reply = normalize_text(re.sub(r"<\|?[\w-]+\|>|<\|[\w-]+>", " ", answer)) or ERROR_REPLY
    except Exception as exc:
        print(f"Error calling Ollama: {exc}")
        tools.counters["model_errors"] += 1
        reply = ERROR_REPLY
    return split_for_mesh(reply, max_bytes, max_reply_parts)
