"""
Copenhagen Water Level Alert Bot
---------------------------------
Fetches the current sea level at Copenhagen (Langelinie station) from the
Danish Meteorological Institute (DMI) Open Data API (no API key required)
and sends a Telegram warning message whenever the level rises above or
drops below 30 cm.

State is saved in state.json so that only *changes* trigger a message —
you won't get spammed every hour while the water is already high/low.
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Configuration — these values come from GitHub Secrets (set in README)
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

# Copenhagen Langelinie tide-gauge station (DMI station ID)
STATION_ID = "30336"

# Alert when the water level goes above +30 cm or below -30 cm (DVR90 datum)
THRESHOLD_CM = 30

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
        return {"is_alert": False, "last_level_cm": None, "last_updated": None}


def save_state(is_alert: bool, level_cm: float) -> None:
    """Persist the current state so the next run can compare."""
    state = {
        "is_alert": is_alert,
        "last_level_cm": round(level_cm, 1),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
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


def send_telegram(message: str) -> None:
    """Send a message to the configured Telegram chat."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "HTML",
    }
    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()
    print("Telegram message sent successfully.")


def format_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def main():
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

    # 4. Send a message only when the state *changes*
    if is_alert and not was_alert:
        # Threshold just crossed — send warning
        direction = "above the upper" if is_high else "below the lower"
        message = (
            f"Hello, here is the current water level in Copenhagen: <b>{level_cm:.1f} cm</b>.\n\n"
            f"⚠️ <b>WARNING:</b> The level is {direction} threshold of ±{THRESHOLD_CM} cm.\n\n"
            f"🕐 {format_timestamp()}"
        )
        send_telegram(message)
        print(f"Alert sent: level={level_cm:.1f} cm")

    elif not is_alert and was_alert:
        # Level returned to normal — send all-clear
        message = (
            f"Hello, here is the current water level in Copenhagen: <b>{level_cm:.1f} cm</b>.\n\n"
            f"✅ The level has returned to normal (within ±{THRESHOLD_CM} cm).\n\n"
            f"🕐 {format_timestamp()}"
        )
        send_telegram(message)
        print(f"All-clear sent: level={level_cm:.1f} cm")

    else:
        status = "ALERT (no change)" if is_alert else "normal"
        print(f"No message sent — status unchanged ({status}).")

    # 5. Persist state for the next run
    save_state(is_alert, level_cm)


if __name__ == "__main__":
    main()
