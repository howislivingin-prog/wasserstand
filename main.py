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
# Configuration — these values come from GitHub Secrets (set in README)
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
    """Load the previous run's state from state.json."""
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"is_alert": False, "last_level_cm": None, "last_updated": None, "last_update_id": None}


def save_state(state: dict) -> None:
    """Persist the current state so the next run can compare."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"State saved: {state}")


def get_water_level_cm() -> float:
    """
    Fetch the latest sea-level observation from the DMI Open Data API.
    No API key required. Returns the value in centimetres relative to DVR90.
    """
    url = "https://opendataapi.dmi.dk/v2/oceanObs/collections/observation/items"
    params = {
        "stationId":   STATION_ID,
        "parameterId": "sealev_dvr",   # Sea level relative to DVR90 (centimetres)
        "limit":       10,
    }

    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    features = data.get("features", [])
    if not features:
        raise ValueError(
            f"No observations returned for station {STATION_ID}. "
            "Check that the station ID is correct."
        )

    # Sort client-side to get the most recent observation
    features.sort(key=lambda f: f["properties"].get("observed", ""), reverse=True)
    return features[0]["properties"]["value"]   # already in centimetres


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


def format_timestamp() -> str:
    now = datetime.now(ZoneInfo("Europe/Copenhagen"))
    return now.strftime("%Y-%m-%d %H:%M")


def build_status_message(level_cm: float) -> str:
    """Build a status message for the current water level."""
    is_high  = level_cm >  THRESHOLD_CM
    is_low   = level_cm < -THRESHOLD_CM
    is_alert = is_high or is_low

    if is_alert:
        direction = "above the upper" if is_high else "below the lower"
        status_line = f"⚠️ <b>WARNING:</b> The level is {direction} threshold of ±{THRESHOLD_CM} cm."
    else:
        status_line = f"✅ The level is normal (within ±{THRESHOLD_CM} cm)."

    return (
        f"Current water level in Copenhagen: <b>{level_cm:.1f} cm</b>\n\n"
        f"{status_line}\n\n"
        f"🕐 {format_timestamp()}"
    )


# ---------------------------------------------------------------------------
# Water level alert logic
# ---------------------------------------------------------------------------

def run_water_level_check():
    print(f"=== Copenhagen Water Level Check — {format_timestamp()} ===")

    # 1. Load previous state
    state = load_state()
    was_alert = state.get("is_alert", False)
    print(f"Previous state: alert={was_alert}, level={state.get('last_level_cm')} cm")

    # 2. Fetch current level
    try:
        level_cm = get_water_level_cm()
    except Exception as exc:
        print(f"ERROR fetching water level: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Current water level: {level_cm:.1f} cm (DVR90)")

    # 3. Determine whether we are in an alert state
    is_high  = level_cm >  THRESHOLD_CM
    is_low   = level_cm < -THRESHOLD_CM
    is_alert = is_high or is_low

    # 4. Send a message every check when beyond threshold; all-clear only once when returning to normal
    if is_alert:
        direction = "above the upper" if is_high else "below the lower"
        message = (
            f"Hello, here is the current water level in Copenhagen: <b>{level_cm:.1f} cm</b>.\n\n"
            f"⚠️ <b>WARNING:</b> The level is {direction} threshold of ±{THRESHOLD_CM} cm.\n\n"
            f"🕐 {format_timestamp()}"
        )
        send_telegram(message)
        print(f"Alert sent: level={level_cm:.1f} cm")

    elif not is_alert and was_alert:
        message = (
            f"Hello, here is the current water level in Copenhagen: <b>{level_cm:.1f} cm</b>.\n\n"
            f"✅ The level has returned to normal (within ±{THRESHOLD_CM} cm).\n\n"
            f"🕐 {format_timestamp()}"
        )
        send_telegram(message)
        print(f"All-clear sent: level={level_cm:.1f} cm")

    else:
        print(f"No message sent — level is normal.")

    # 5. Persist state for the next run (preserve last_update_id)
    state["is_alert"] = is_alert
    state["last_level_cm"] = round(level_cm, 1)
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


# ---------------------------------------------------------------------------
# Telegram command handler
# ---------------------------------------------------------------------------

def run_command_handler():
    print(f"=== Telegram Command Check — {format_timestamp()} ===")

    state = load_state()
    last_update_id = state.get("last_update_id")

    # Fetch new updates from Telegram
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
        text = message.get("text", "").strip()
        chat_id = str(message.get("chat", {}).get("id", ""))

        if text.startswith("/update"):
            print(f"Handling /update command from chat {chat_id}")
            try:
                level_cm = get_water_level_cm()
                reply = build_status_message(level_cm)
                send_telegram(reply, chat_id=chat_id)
            except Exception as exc:
                print(f"ERROR responding to /update: {exc}", file=sys.stderr)

    # Save updated last_update_id (preserve alert state)
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
