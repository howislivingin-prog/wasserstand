"""
Copenhagen Water Level & Water Quality Alert Bot
-------------------------------------------------
Fetches sea level from DMI and water quality from badevand.dk,
then sends Telegram alerts when thresholds are crossed.

Run modes:
  python main.py               — water level + quality check & alert
  python main.py --commands    — respond to /update commands in Telegram
"""

import json
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

STATION_ID   = "30336"   # Copenhagen Langelinie tide-gauge
THRESHOLD_CM = 5         # Alert threshold in cm (DVR90)
STATE_FILE   = "state.json"

# Copenhagen harbour beaches to monitor (closest to Sluseholmen first)
CPH_BEACHES = [
    "Teglværkshavnen",
    "Fisketorvet",
    "Islands Brygge",
    "Gasværkshavnen",
    "Sluseholmen",
]

# Water quality levels from badevand.dk
QUALITY_LABELS = {
    1: ("✅", "Good"),
    2: ("⚠️", "Warning"),
    3: ("🚫", "Bad — do not swim"),
    4: ("🔒", "Closed"),
}


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
        state.setdefault("water_quality_alert", False)
        return state
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "is_alert": False,
            "last_level_cm": None,
            "last_updated": None,
            "last_update_id": None,
            "water_quality_alert": False,
        }


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


# ---------------------------------------------------------------------------
# Sea level
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Water quality via badevand.dk (Playwright intercepts the browser request)
# ---------------------------------------------------------------------------

def get_water_quality() -> list:
    """
    Uses a headless browser to visit badevand.dk and intercepts the
    /api/next/beaches response (which requires reCAPTCHA v3).
    Returns a filtered list of relevant Copenhagen harbour beaches.
    """
    from playwright.sync_api import sync_playwright

    beaches_data = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        def handle_response(response):
            if "/api/next/beaches" in response.url:
                try:
                    data = response.json()
                    if isinstance(data, list):
                        beaches_data.extend(data)
                        print(f"Intercepted {len(data)} beaches from badevand.dk")
                except Exception as exc:
                    print(f"Could not parse beaches response: {exc}")

        page.on("response", handle_response)

        try:
            page.goto("https://badevand.dk", wait_until="networkidle", timeout=45000)
        except Exception as exc:
            print(f"Page load note: {exc}")

        browser.close()

    # Filter for relevant Copenhagen harbour beaches
    relevant = []
    for beach in beaches_data:
        name = beach.get("name", "") or ""
        if any(b.lower() in name.lower() for b in CPH_BEACHES):
            quality_val = beach.get("waterQuality") or beach.get("water_quality")
            relevant.append({
                "name":    name,
                "quality": quality_val,
            })

    print(f"Found {len(relevant)} relevant beaches: {[b['name'] for b in relevant]}")
    return relevant


def is_water_quality_bad(beaches: list) -> bool:
    """Returns True if any monitored beach has bad (3) or closed (4) quality."""
    for beach in beaches:
        q = beach.get("quality")
        if q is not None and q >= 3:
            return True
    return False


def format_quality_line(beach: dict) -> str:
    q = beach.get("quality")
    if q is None:
        return f"  • {beach['name']}: unknown"
    emoji, label = QUALITY_LABELS.get(q, ("❓", f"Unknown ({q})"))
    return f"  • {beach['name']}: {emoji} {label}"


# ---------------------------------------------------------------------------
# Status message for /update
# ---------------------------------------------------------------------------

def build_status_message(level_cm: float, beaches: list = None) -> str:
    is_high  = level_cm >  THRESHOLD_CM
    is_low   = level_cm < -THRESHOLD_CM
    is_alert = is_high or is_low

    if is_alert:
        direction = "above the upper" if is_high else "below the lower"
        level_status = f"⚠️ The level is {direction} threshold of ±{THRESHOLD_CM} cm."
    else:
        level_status = f"✅ Normal (within ±{THRESHOLD_CM} cm)."

    msg = (
        f"🌊 <b>Sea level:</b> {level_cm:.1f} cm\n"
        f"{level_status}"
    )

    if beaches:
        msg += "\n\n🏊 <b>Water quality:</b>\n"
        msg += "\n".join(format_quality_line(b) for b in beaches)

    msg += f"\n\n🕐 {format_timestamp()}"
    return msg


# ---------------------------------------------------------------------------
# Water level + quality alert logic
# ---------------------------------------------------------------------------

def run_water_level_check():
    print(f"=== Copenhagen Water Level & Quality Check — {format_timestamp()} ===")

    state = load_state()
    was_level_alert   = state.get("is_alert", False)
    was_quality_alert = state.get("water_quality_alert", False)

    # --- Sea level ---
    try:
        level_cm = get_water_level_cm()
    except Exception as exc:
        print(f"ERROR fetching water level: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Current water level: {level_cm:.1f} cm (DVR90)")

    is_high        = level_cm >  THRESHOLD_CM
    is_low         = level_cm < -THRESHOLD_CM
    is_level_alert = is_high or is_low

    if is_level_alert:
        direction = "above the upper" if is_high else "below the lower"
        message = (
            f"🌊 <b>Sea Level Warning — Copenhagen</b>\n\n"
            f"Current level: <b>{level_cm:.1f} cm</b>\n"
            f"⚠️ The level is {direction} threshold of ±{THRESHOLD_CM} cm.\n\n"
            f"🕐 {format_timestamp()}"
        )
        send_telegram(message)
        print(f"Alert sent: level={level_cm:.1f} cm")
    elif not is_level_alert and was_level_alert:
        message = (
            f"🌊 <b>Sea Level Update — Copenhagen</b>\n\n"
            f"Current level: <b>{level_cm:.1f} cm</b>\n"
            f"✅ The level has returned to normal (within ±{THRESHOLD_CM} cm).\n\n"
            f"🕐 {format_timestamp()}"
        )
        send_telegram(message)
        print(f"All-clear sent: level={level_cm:.1f} cm")
    else:
        print("Sea level: normal, no message sent.")

    # --- Water quality ---
    try:
        beaches = get_water_quality()
    except Exception as exc:
        print(f"ERROR fetching water quality: {exc}", file=sys.stderr)
        beaches = []

    is_quality_alert = is_water_quality_bad(beaches)

    if beaches:
        quality_lines = "\n".join(format_quality_line(b) for b in beaches)

        if is_quality_alert:
            header = "⚠️ Poor water quality detected at:"
        else:
            header = "✅ Water quality is good at:"

        message = (
            f"🏊 <b>Water Quality — Copenhagen Harbour</b>\n\n"
            f"{header}\n{quality_lines}\n\n"
            f"🕐 {format_timestamp()}"
        )
        send_telegram(message)
        print(f"Water quality message sent (alert={is_quality_alert}).")

    # --- Save state ---
    state["is_alert"]            = is_level_alert
    state["water_quality_alert"] = is_quality_alert
    state["last_level_cm"]       = round(level_cm, 1)
    state["last_updated"]        = datetime.now(timezone.utc).isoformat()
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
                try:
                    beaches = get_water_quality()
                except Exception as exc:
                    print(f"Water quality unavailable: {exc}")
                    beaches = []
                reply = build_status_message(level_cm, beaches)
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
