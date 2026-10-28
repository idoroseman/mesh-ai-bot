import asyncio
import csv
import json
import os
import time
from typing import Any, Dict, List

from meshcore import MeshCore

from config import CONFIG

# Repeater names (or parts of names) to probe; empty means every repeater the radio knows
PROBE_REPEATERS: List[str] = CONFIG.get("meshcore_probe_repeaters", [])
PROBE_INTERVAL_SECONDS = CONFIG.get("meshcore_probe_interval_seconds", 3600)
PROBE_FILE = CONFIG.get("meshcore_probe_file", "repeater_telemetry.csv")
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


def flatten(value: Any, prefix: str, row: Dict[str, Any]) -> None:
    """Spread nested status/telemetry values into columns such as status_bat or telemetry_voltage_1."""
    if isinstance(value, dict):
        for key, item in value.items():
            flatten(item, f"{prefix}_{key}", row)
    elif isinstance(value, list) and all(isinstance(i, dict) and "type" in i for i in value):
        # Cayenne LPP readings: [{"channel": 1, "type": "voltage", "value": 4.1}, ...]
        for item in value:
            flatten(item.get("value"), f"{prefix}_{item['type']}_{item.get('channel', '')}".rstrip("_"), row)
    elif isinstance(value, list):
        row[prefix] = json.dumps(value, ensure_ascii=False, default=str)
    else:
        row[prefix] = value


def write_probe_row(path: str, record: Dict[str, Any]) -> None:
    """Append a row to the CSV, rewriting it with a wider header when a new column shows up."""
    row: Dict[str, Any] = {}
    for key, value in record.items():
        if key in ("status", "telemetry"):
            if value is not None:
                flatten(value, key, row)
        else:
            row[key] = value

    rows: List[Dict[str, Any]] = []
    header: List[str] = []
    if os.path.exists(path) and os.path.getsize(path):
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            header = list(reader.fieldnames or [])
            if any(key not in header for key in row):
                rows = list(reader)
    new_columns = [key for key in row if key not in header]

    if header and not new_columns:
        with open(path, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=header).writerow(row)
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header + new_columns)
        writer.writeheader()
        writer.writerows(rows + [row])


async def probe_forever(meshcore: MeshCore) -> None:
    """Append one CSV row per repeater to PROBE_FILE every PROBE_INTERVAL_SECONDS."""
    while True:
        try:
            for contact in select_repeaters(meshcore.contacts, PROBE_REPEATERS):
                record = await probe_repeater(meshcore, contact)
                print(f"Probed repeater {record['repeater']}: {record.get('error', 'ok')}")
                write_probe_row(PROBE_FILE, record)
        except Exception as exc:
            print(f"Repeater probe failed: {exc!r}")
        await asyncio.sleep(PROBE_INTERVAL_SECONDS)


# The CSV columns the status page shows, with their headings
SUMMARY_COLUMNS = {
    "repeater": "repeater",
    "time": "probed",
    "status_bat": "battery mV",
    "telemetry_voltage_1": "voltage",
    "telemetry_temperature_1": "temp °C",
    "status_noise_floor": "noise floor",
    "status_last_rssi": "last RSSI",
    "status_last_snr": "last SNR",
    "status_uptime": "uptime s",
    "status_tx_queue_len": "tx queue",
    "error": "error",
}


def latest_rows(path: str = PROBE_FILE) -> List[Dict[str, Any]]:
    """The newest probe of each repeater, cut down to SUMMARY_COLUMNS."""
    try:
        with open(path, newline="", encoding="utf-8") as f:
            latest = {row.get("repeater", ""): row for row in csv.DictReader(f)}
    except FileNotFoundError:
        return []
    return [
        {heading: row.get(column) or "" for column, heading in SUMMARY_COLUMNS.items()}
        for _, row in sorted(latest.items())
    ]


# The CSV columns the graphs plot
HISTORY_COLUMNS = {
    "battery": "status_bat",
    "voltage": "telemetry_voltage_1",
    "temperature": "telemetry_temperature_1",
}


def number(value: str | None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def history(path: str = PROBE_FILE) -> List[Dict[str, Any]]:
    """Every probe that has a battery, voltage or temperature reading, grouped by repeater, oldest first."""
    series: Dict[str, List[Dict[str, Any]]] = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    when = time.mktime(time.strptime(row.get("time", ""), "%Y-%m-%dT%H:%M:%S"))
                except ValueError:
                    continue
                values = {key: number(row.get(column)) for key, column in HISTORY_COLUMNS.items()}
                if any(v is not None for v in values.values()):
                    series.setdefault(row.get("repeater", ""), []).append({"t": int(when * 1000), **values})
    except FileNotFoundError:
        return []
    return [{"name": name, "points": sorted(points, key=lambda p: p["t"])} for name, points in sorted(series.items())]
