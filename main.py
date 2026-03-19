"""
Copenhagen Water Level & Water Quality Alert Bot
-------------------------------------------------
Fetches the current sea level at Copenhagen (Langelinie station) from the
Danish Meteorological Institute (DMI) Open Data API (no API key required)
and sends a Telegram warning whenever the level rises above or drops below
the set threshold.

Also checks bathing water quality for the beach nearest to Sluseholmen
using the DHI Vandudsigten API (no API key required) and sends a warning
when swimming is not recommended.

Run modes:
  python main.py               — water level check + water quality check
  python main.py --commands    — respond to /update commands in Telegram
"""

import json
import math
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------------------
# Configuration — these values come from GitHub Secrets
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

# Copenhagen Langelinie tide-gauge station (DMI station ID)
STATION_ID = "30336"

# Alert when the water level goes above or below this threshold (cm, DVR90)
THRESHOLD_CM = 5

# Sluseholmen coordinates — used to find the nearest bathing beach
SLUSEHOLMEN_LAT = 55.656
SLUSEHOLMEN_LON = 12.528

# Water quality API (DHI Vandudsigten / badevand.dk — no API key required)
WATER_QUALITY_API_URL = "http://api.vandudsigten.dk/beaches/"

# Quality scale: 1 = excellent, 2 = good, 3 = poor (red flag), 4 = no data
WATER_QUALITY_LABELS = {
    1: "🟢 Excellent — safe to swim",
    2: "🟢 Good — safe to swim",
    3: "🔴 Poor — swimming not recommended",
    4: "⬛ No data available",
}

# File that stores the last known state (committed back to the repo)
STATE_FILE = "state.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """Load the previous run's state from state.json."""
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        # Ensure all expected keys exist (handles old state files)
        state.setdefault("is_alert", False)
        state.setdefault("last_level_cm", None)
        state.setdefault("last_updated", None)
        state.setdefault("last_update_id", None)
        state.setdefault("is_water_quality_alert", False)
        state.setdefault("last_water_quality", None)
        return state
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "is_alert": False,
            "last_level_cm": None,
            "last_updated": None,
            "last_update_id": None,
            "is_water_quality_alert": False,
            "last_water_quality": None,
        }


def save_state(state: dict) -> None:
    """Persist the current state so the next run can compare."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"State saved: {state}")


def format_timestamp() -> str:
    now = datetime.now(ZoneInfo("Europe/Copenhagen"))
    return now.strftime("%Y-%m-%d %H:%M")


def send_telegram(message: str, chat_id: str = None) -> None:
    """Send a message to the configured Telegram chat (or a specific chat)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    chat_id or TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "HTML",
    }
    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()
    print("Telegram message sent successfully.")


# ---------------------------------------------------------------------------
# Water level
# ---------------------------------------------------------------------------

def get_water_level_cm() -> float:
    """
    Fetch the latest sea-level observation from the DMI Open Data API.
    No API key required. Returns the value in centimetres relative to DVR90.
    """
    url = "https://opendataapi.dmi.dk/v2/oceanObs/collections/observation/items"
    params = {
        "stationId":   STATION_ID,
        "parameterId": "sealev_dvr",
        "limit":       10,
    }
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    features = response.json().get("features", [])
    if not features:
        raise ValueError(f"No observations returned for station {STATION_ID}.")
    features.sort(key=lambda f: f["properties"].get("observed", ""), reverse=True)
    return features[0]["properties"]["value"]


def run_water_level_check(state: dict) -> dict:
    """Check water level, send alerts if needed, and return updated state."""
    print(f"=== Copenhagen Water Level Check — {format_timestamp()} ===")

    was_alert = state.get("is_alert", False)
    print(f"Previous state: alert={was_alert}, level={state.get('last_level_cm')} cm")

    try:
        level_cm = get_water_level_cm()
    except Exception as exc:
        print(f"ERROR fetching water level: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Current water level: {level_cm:.1f} cm (DVR90)")

    is_high  = level_cm >  THRESHOLD_CM
    is_low   = level_cm < -THRESHOLD_CM
    is_alert = is_high or is_low

    if is_alert:
        direction = "above the upper" if is_high else "below the lower"
        message = (
            f"🌊 <b>Sea Level Warning — Copenhagen</b>\n\n"
            f"Current level: <b>{level_cm:.1f} cm</b>\n"
            f"⚠️ The level is {direction} threshold of ±{THRESHOLD_CM} cm.\n\n"
            f"🕐 {format_timestamp()}"
        )
        send_telegram(message)
        print(f"Alert sent: level={level_cm:.1f} cm")

    elif not is_alert and was_alert:
        message = (
            f"🌊 <b>Sea Level Update — Copenhagen</b>\n\n"
            f"Current level: <b>{level_cm:.1f} cm</b>\n"
            f"✅ The level has returned to normal (within ±{THRESHOLD_CM} cm).\n\n"
            f"🕐 {format_timestamp()}"
        )
        send_telegram(message)
        print(f"All-clear sent: level={level_cm:.1f} cm")

    else:
        print("No message sent — level is normal.")

    state["is_alert"] = is_alert
    state["last_level_cm"] = round(level_cm, 1)
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    return state


# ---------------------------------------------------------------------------
# Water quality
# ---------------------------------------------------------------------------

def get_water_quality() -> tuple:
    """
    Fetch today's bathing water quality for the beach nearest to Sluseholmen.
    Returns (quality_int, beach_name).
    Quality: 1=excellent, 2=good, 3=poor/red flag, 4=no data.
    """
    response = requests.get(WATER_QUALITY_API_URL, timeout=30, allow_redirects=True)
    response.raise_for_status()
    beaches = response.json()

    # Filter to Copenhagen beaches with coordinates
    cph_beaches = [
        b for b in beaches
        if b.get("municipality") == "København"
        and b.get("latitude") and b.get("longitude")
    ]
    if not cph_beaches:
        raise ValueError("No Copenhagen beaches found in water quality API.")

    # Find the beach closest to Sluseholmen
    def dist(b):
        return math.sqrt(
            (b["latitude"]  - SLUSEHOLMEN_LAT) ** 2 +
            (b["longitude"] - SLUSEHOLMEN_LON) ** 2
        )

    closest    = min(cph_beaches, key=dist)
    beach_name = closest["name"]

    data = closest.get("data", [])
    if not data or data[0].get("water_quality") == "":
        return (4, beach_name)

    return (int(data[0]["water_quality"]), beach_name)


def run_water_quality_check(state: dict) -> dict:
    """Check water quality, send alerts if needed, and return updated state."""
    print(f"=== Copenhagen Water Quality Check — {format_timestamp()} ===")

    was_quality_alert = state.get("is_water_quality_alert", False)
    print(f"Previous state: water_quality_alert={was_quality_alert}, quality={state.get('last_water_quality')}")

    try:
        quality, beach_name = get_water_quality()
    except Exception as exc:
        print(f"ERROR fetching water quality: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Current water quality at {beach_name}: {quality} ({WATER_QUALITY_LABELS.get(quality, '?')})")

    is_quality_alert = (quality == 3)

    if is_quality_alert:
        message = (
            f"🏊 <b>Water Quality Warning — {beach_name}</b>\n\n"
            f"⚠️ Water quality is currently <b>POOR</b>.\n"
            f"Swimming is not recommended.\n\n"
            f"🕐 {format_timestamp()}"
        )
        send_telegram(message)
        print(f"Water quality alert sent: quality={quality}")

    elif not is_quality_alert and was_quality_alert:
        label = WATER_QUALITY_LABELS.get(quality, "Unknown")
        message = (
            f"🏊 <b>Water Quality Update — {beach_name}</b>\n\n"
            f"✅ Water quality has improved: {label}.\n"
            f"Swimming is now safe.\n\n"
            f"🕐 {format_timestamp()}"
        )
        send_telegram(message)
        print(f"Water quality all-clear sent: quality={quality}")

    else:
        print("No water quality message sent — quality is normal.")

    state["is_water_quality_alert"] = is_quality_alert
    state["last_water_quality"] = quality
    return state


# ---------------------------------------------------------------------------
# /update command response builder
# ---------------------------------------------------------------------------

def build_full_status_message() -> str:
    """Fetch current water level + water quality and build a combined reply."""
    lines = []

    # Water level
    try:
        level_cm = get_water_level_cm()
        is_high  = level_cm >  THRESHOLD_CM
        is_low   = level_cm < -THRESHOLD_CM
        if is_high or is_low:
            direction = "above the upper" if is_high else "below the lower"
            level_status = f"⚠️ The level is {direction} threshold of ±{THRESHOLD_CM} cm."
        else:
            level_status = f"✅ Normal (within ±{THRESHOLD_CM} cm)."
        lines.append(f"🌊 <b>Sea level:</b> {level_cm:.1f} cm\n{level_status}")
    except Exception as exc:
        lines.append(f"🌊 <b>Sea level:</b> could not fetch data ({exc})")

    lines.append("")

    # Water quality
    try:
        quality, beach_name = get_water_quality()
        quality_label = WATER_QUALITY_LABELS.get(quality, "Unknown")
        lines.append(f"🏊 <b>Water quality at {beach_name}:</b>\n{quality_label}")
    except Exception as exc:
        lines.append(f"🏊 <b>Water quality:</b> could not fetch data ({exc})")

    lines.append("")
    lines.append(f"🕐 {format_timestamp()}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram command handler
# ---------------------------------------------------------------------------

def run_command_handler():
    print(f"=== Telegram Command Check — {format_timestamp()} ===")

    state = load_state()
    last_update_id = state.get("last_update_id")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 0, "allowed_updates": ["message"]}
    if last_update_id is not None:
        params["offset"] = last_update_id + 1

    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        updates = response.json().get("result", [])
    except Exception as exc:
        print(f"ERROR fetching Telegram updates: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Received {len(updates)} new update(s).")

    for update in updates:
        last_update_id = update["update_id"]
        message = update.get("message", {})
        text    = message.get("text", "").strip()
        chat_id = str(message.get("chat", {}).get("id", ""))

        if text.startswith("/update"):
            print(f"Handling /update command from chat {chat_id}")
            try:
                reply = build_full_status_message()
                send_telegram(reply, chat_id=chat_id)
            except Exception as exc:
                print(f"ERROR responding to /update: {exc}", file=sys.stderr)

    state["last_update_id"] = last_update_id
    save_state(state)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--commands" in sys.argv:
        run_command_handler()
    else:
        state = load_state()
        state = run_water_level_check(state)
        state = run_water_quality_check(state)
        save_state(state)
