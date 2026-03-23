"""
Copenhagen Water Level Alert Bot
---------------------------------
Fetches the current sea level at Copenhagen (Langelinie station) from the
Danish Meteorological Institute (DMI) Open Data API (no API key required)
and sends a Telegram warning message whenever the level rises above or
drops below the set threshold.

Also fetches the DMI DKSS storm surge forecast to warn in advance when
the threshold is expected to be crossed within the next 6 hours.

Run modes:
  python main.py               — water level check + alert
  python main.py --commands    — respond to /update commands in Telegram
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------------------
# Configuration — these values come from GitHub Secrets
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

# Copenhagen Langelinie tide-gauge station (DMI station ID)
STATION_ID = "30336"

# Copenhagen coordinates (for forecast lookup)
CPH_LON = 12.5988
CPH_LAT = 55.7042

# Alert when the water level goes above or below this threshold (cm, DVR90)
THRESHOLD_CM = 30

# Send a pre-warning when forecast predicts threshold breach within this many hours
FORECAST_WARNING_HOURS = 6

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
        state.setdefault("forecast_warning_sent", False)
        return state
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "is_alert": False,
            "last_level_cm": None,
            "last_updated": None,
            "last_update_id": None,
            "forecast_warning_sent": False,
        }


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"State saved: {state}")


def format_timestamp() -> str:
    now = datetime.now(ZoneInfo("Europe/Copenhagen"))
    return now.strftime("%Y-%m-%d %H:%M")


def format_dt(dt: datetime) -> str:
    """Format a UTC datetime in Copenhagen local time."""
    return dt.astimezone(ZoneInfo("Europe/Copenhagen")).strftime("%Y-%m-%d %H:%M")


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
    """Fetch the latest sea-level observation from DMI. Returns cm (DVR90)."""
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


def get_forecast() -> list[tuple[datetime, float]]:
    """
    Fetch the DMI DKSS storm surge forecast for Copenhagen.
    Returns a list of (datetime, level_cm) tuples sorted by time,
    covering approximately the next 48 hours.
    """
    url = "https://opendataapi.dmi.dk/v1/forecastedr/collections/dkss_idw/position"
    params = {
        "coords":           f"POINT({CPH_LON} {CPH_LAT})",
        "parameter-name":   "sea-mean-deviation",
        "f":                "GeoJSON",
    }
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    features = response.json().get("features", [])

    now = datetime.now(timezone.utc)
    points = []
    for f in features:
        props = f.get("properties", {})
        t = props.get("step")
        val = props.get("sea-mean-deviation")
        if t is not None and val is not None:
            dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
            if dt >= now:
                points.append((dt, round(float(val) * 100, 1)))  # metres → cm

    points.sort(key=lambda x: x[0])
    return points


def get_forecast_peak(forecast: list[tuple[datetime, float]]) -> tuple[float, datetime] | None:
    """Return (peak_cm, peak_time) for the highest absolute value in the next 24 hours."""
    if not forecast:
        return None
    now = datetime.now(timezone.utc)
    window = [x for x in forecast if x[0] <= now + timedelta(hours=24)]
    if not window:
        return None
    peak_dt, peak_cm = max(window, key=lambda x: abs(x[1]))
    return peak_cm, peak_dt


def forecast_line(forecast: list[tuple[datetime, float]]) -> str:
    """Build a one-line forecast summary for messages (next 24h peak)."""
    if not forecast:
        return ""
    result = get_forecast_peak(forecast)
    if not result:
        return ""
    peak_cm, peak_dt = result
    now = datetime.now(timezone.utc)
    hours_away = (peak_dt - now).total_seconds() / 3600
    return f"📈 24h forecast peak: <b>{peak_cm:+.1f} cm</b> in ~{hours_away:.0f}h ({format_dt(peak_dt)})"


def build_status_message(level_cm: float, forecast: list[tuple[datetime, float]] | None = None) -> str:
    is_high  = level_cm >  THRESHOLD_CM
    is_low   = level_cm < -THRESHOLD_CM
    is_alert = is_high or is_low

    if is_alert:
        direction = "above the upper" if is_high else "below the lower"
        status = f"⚠️ The level is {direction} threshold of ±{THRESHOLD_CM} cm."
    else:
        status = f"✅ Normal (within ±{THRESHOLD_CM} cm)."

    parts = [
        f"🌊 <b>Sea level:</b> {level_cm:.1f} cm",
        status,
    ]
    if forecast:
        parts.append(forecast_line(forecast))
    parts.append(f"🕐 {format_timestamp()}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Water level alert logic
# ---------------------------------------------------------------------------

def run_water_level_check():
    print(f"=== Copenhagen Water Level Check — {format_timestamp()} ===")

    state = load_state()
    was_alert              = state.get("is_alert", False)
    forecast_warning_sent  = state.get("forecast_warning_sent", False)
    print(f"Previous state: alert={was_alert}, forecast_warning={forecast_warning_sent}, level={state.get('last_level_cm')} cm")

    try:
        level_cm = get_water_level_cm()
    except Exception as exc:
        print(f"ERROR fetching water level: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Current water level: {level_cm:.1f} cm (DVR90)")

    # Fetch forecast (non-fatal if it fails)
    forecast = []
    try:
        forecast = get_forecast()
        print(f"Forecast: {len(forecast)} points fetched")
    except Exception as exc:
        print(f"WARNING: could not fetch forecast: {exc}", file=sys.stderr)

    is_high  = level_cm >  THRESHOLD_CM
    is_low   = level_cm < -THRESHOLD_CM
    is_alert = is_high or is_low

    if is_alert:
        # Threshold already crossed — send warning (every 3h as long as in alert)
        direction = "above the upper" if is_high else "below the lower"
        fline = f"\n{forecast_line(forecast)}" if forecast else ""
        message = (
            f"🌊 <b>Sea Level Warning — Copenhagen</b>\n\n"
            f"Current level: <b>{level_cm:.1f} cm</b>\n"
            f"⚠️ The level is {direction} threshold of ±{THRESHOLD_CM} cm."
            f"{fline}\n\n"
            f"🕐 {format_timestamp()}"
        )
        send_telegram(message)
        print(f"Alert sent: level={level_cm:.1f} cm")
        # Reset pre-warning flag since we're already in full alert
        state["forecast_warning_sent"] = False

    elif not is_alert and was_alert:
        # Just returned to normal — send all-clear
        fline = f"\n{forecast_line(forecast)}" if forecast else ""
        message = (
            f"🌊 <b>Sea Level Update — Copenhagen</b>\n\n"
            f"Current level: <b>{level_cm:.1f} cm</b>\n"
            f"✅ The level has returned to normal (within ±{THRESHOLD_CM} cm)."
            f"{fline}\n\n"
            f"🕐 {format_timestamp()}"
        )
        send_telegram(message)
        print(f"All-clear sent: level={level_cm:.1f} cm")
        state["forecast_warning_sent"] = False

    else:
        # Normal — check if forecast predicts a breach within FORECAST_WARNING_HOURS
        now = datetime.now(timezone.utc)
        warning_window = now + timedelta(hours=FORECAST_WARNING_HOURS)
        upcoming_breach = next(
            ((dt, cm) for dt, cm in forecast
             if dt <= warning_window and abs(cm) >= THRESHOLD_CM),
            None
        )

        if upcoming_breach and not forecast_warning_sent:
            breach_dt, breach_cm = upcoming_breach
            hours_away = (breach_dt - now).total_seconds() / 3600
            direction = "above" if breach_cm > 0 else "below"
            message = (
                f"📢 <b>Sea Level Forecast Warning — Copenhagen</b>\n\n"
                f"Current level: <b>{level_cm:.1f} cm</b> (normal)\n"
                f"⚠️ Forecast shows <b>{breach_cm:+.1f} cm</b> in ~{hours_away:.0f}h "
                f"({format_dt(breach_dt)}) — {direction} the ±{THRESHOLD_CM} cm threshold.\n\n"
                f"🕐 {format_timestamp()}"
            )
            send_telegram(message)
            print(f"Pre-warning sent: forecast breach at {breach_dt} ({breach_cm:+.1f} cm)")
            state["forecast_warning_sent"] = True

        elif not upcoming_breach:
            print("No message sent — level normal, no forecast breach expected.")
            state["forecast_warning_sent"] = False
        else:
            print("No message sent — pre-warning already sent.")

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
                forecast = []
                try:
                    forecast = get_forecast()
                except Exception:
                    pass
                reply = build_status_message(level_cm, forecast)
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
