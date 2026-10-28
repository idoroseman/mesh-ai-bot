# mesh-ai-bot

An AI assistant for LoRa mesh networks. It listens on a MeshCore (or Meshtastic) radio, answers channel and direct messages with a local [Ollama](https://ollama.com) model, and can look up live mesh data through tools.

## Features

- Replies to messages on channels whose name contains "bot", to direct messages, and to `@bot` mentions elsewhere.
- Splits replies into mesh-sized parts (`max_message_bytes`, `max_reply_parts`).
- Model tools: ping, link test, contact list, channels, recent network events and bot health.
- Community info for the system prompt comes from `background_meshcore.md` / `background_meshtastic.md`, re-read on every message.
- MeshCore only:
  - Sends an advert at startup.
  - Uploads the repeaters, rooms and sensors it hears to [map.meshcore.io](https://map.meshcore.io), signed with the radio's key (`meshcore_map_upload`).
  - Acts as a [MeshMapper](https://wiki.meshmapper.net/mqtt-main/) MQTT observer, publishing every packet it hears to `meshcore/<IATA>/<pubkey>/packets` (`meshcore_mqtt_observer`).
  - Probes repeaters every hour and appends status and telemetry to a JSONL file.

## Files

| File | Purpose |
| --- | --- |
| `bot_meshcore.py` | MeshCore entry point |
| `repeater_telemetry.py` | Periodic repeater status/telemetry probe, CSV logging |
| `map_upload.py` | Signed advert uploads to map.meshcore.io |
| `status_server.py`, `templates/`, `static/` | Flask status page (one tab per section, repeater graphs) and JSON API |
| `mqtt_observer.py` | MeshMapper MQTT observer (packets and status) |
| `bot_meshtastic.py` | Meshtastic entry point |
| `bot.py` | System prompt, model calls, reply splitting |
| `tools.py`, `tools_meshcore.py`, `tools_meshtastic.py` | Model tools |
| `config.example.json` | Template for `config.json` |
| `mesh-ai-bot.service` | systemd unit for a Raspberry Pi |
| `test_bot.py` | Unit tests with a fake radio and model |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install meshcore meshtastic ollama pyserial paho-mqtt flask
cp config.example.json config.json
```

Edit `config.json`, then pull the model on your Ollama host (`ollama pull <model_name>`).

## Configuration

All settings live in `config.json`, which is git-ignored because it can hold a password.

| Key | Meaning |
| --- | --- |
| `ollama_host`, `model_name` | Ollama server and model; the model should support tool calling |
| `meshcore_port` / `meshcore_ble` | Serial port or Bluetooth name of the companion radio; Bluetooth wins when set |
| `meshcore_map_upload` | Upload heard repeaters to the MeshCore map; needs a radio that exports its private key |
| `meshcore_mqtt_observer`, `meshcore_mqtt_iata` | Publish heard packets to MeshMapper's broker under this region code; needs the key export. `meshcore_mqtt_server` / `meshcore_mqtt_port` override `mqtt.meshmapper.net:443` |
| `meshcore_repeater_password` | Guest or admin password used to log in to repeaters before probing |
| `meshcore_probe_repeaters` | Repeater names (or parts) to probe; empty means every known repeater |
| `meshcore_probe_interval_seconds`, `meshcore_probe_file` | Probe period (default 3600) and output file |
| `status_port`, `status_host` | Status page at `http://<host>:<port>/`, with repeater battery/voltage/temperature graphs at `/graphs` (JSON at `/api/status`, `/api/telemetry`); 0 turns it off. It has no login, so keep it on your LAN |
| `meshtastic_port` / `meshtastic_ble` | Meshtastic radio connection |
| `max_message_bytes`, `max_reply_parts`, `part_delay_seconds` | Reply size and pacing |
| `bot_language`, `bot_mentions`, `error_reply` | Language, mention words, fallback reply |
| `*_background_path`, `max_background_chars` | Community background files |
| `max_tool_rounds`, `max_tool_result_chars`, `max_listed_contacts`, `max_network_events` | Tool limits |

Probing every repeater on the mesh uses a lot of airtime, so set `meshcore_probe_repeaters` to your own.

## Run

```bash
.venv/bin/python bot_meshcore.py     # MeshCore
.venv/bin/python bot_meshtastic.py   # Meshtastic
```

## Run as a service (Raspberry Pi)

Adjust `User`, `WorkingDirectory` and `ExecStart` in `mesh-ai-bot.service` if the project is not in `/home/admin/mesh-ai-bot`, then:

```bash
sudo cp mesh-ai-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mesh-ai-bot
journalctl -u mesh-ai-bot -f
```

## MeshCore notes

- Direct messages need both radios to know each other. Flood adverts (`send_advert(flood=True)`) make the bot reachable beyond direct range, and the bot's radio must be allowed to add contacts automatically.
- The bot is sent only a zero-hop advert at startup by default.

## Tests

```bash
.venv/bin/python -m unittest -v
```
