"""
Copenhagen Water Level Alert Bot
---------------------------------
Fetches the current sea level at Copenhagen (Langelinie station) from the
Danish Meteorological Institute (DMI) Open Data API (no API key required)
and sends a Telegram warning message whenever the level rises above or
drops below the set threshold.

Run modes:
  python main.py               — water level check + alert
  python main.py --commands    — respond to /update commands in Telegram
"""

import json
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

# File that stores the last known state (committed back to the repo)
STATE_FILE = "state.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        state.setdefault("is_alert", False)
        state.setdefault("last_level_cm", None)
        state.setdefault("last_updated", None)
        state.setdefault("last_update_id", None)
        return state
    except (FileNotFoundError, json.JSONDecodeError):
        return {"is_alert": False, "last_level_cm": None, "last_updated": None, "last_update_id": None}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"State saved: {state}")


def format_timestamp() -> str:
    now = datetime.now(ZoneInfo("Europe/Copenhagen"))
    return now.strftime("%Y-%m-%d %H:%M")


def send_telegram(message: str, chat_id: str = None) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    chat_id or TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "HTML",
    }
    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()
    print("Telegram message sent successfully.")


def get_water_level_cm() -> float:
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


def build_status_message(level_cm: float) -> str:
    is_high  = level_cm >  THRESHOLD_CM
    is_low   = level_cm < -THRESHOLD_CM
    is_alert = is_high or is_low

    if is_alert:
        direction = "above the upper" if is_high else "below the lower"
        status = f"⚠️ The level is {direction} threshold of ±{THRESHOLD_CM} cm."
    else:
        status = f"✅ Normal (within ±{THRESHOLD_CM} cm)."

    return (
        f"🌊 <b>Sea level:</b> {level_cm:.1f} cm\n"
        f"{status}\n\n"
        f"🕐 {format_timestamp()}"
    )


# ---------------------------------------------------------------------------
# Water level alert logic
# ---------------------------------------------------------------------------

def run_water_level_check():
    print(f"=== Copenhagen Water Level Check — {format_timestamp()} ===")

    state = load_state()
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

    state["is_alert"]      = is_alert
    state["last_level_cm"] = round(level_cm, 1)
    state["last_updated"]  = datetime.now(timezone.utc).isoformat()
    save_state(state)


# ---------------------------------------------------------------------------
# Telegram command handler
# ---------------------------------------------------------------------------

def run_command_handler():
    print(f"=== Telegram Command Check — {format_timestamp()} ===")

    state = load_state()
    last_update_id = state.get("last_update_id")

    url    = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
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
                level_cm = get_water_level_cm()
                reply = build_status_message(level_cm)
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
        run_water_level_check()
