# Copenhagen Water Level Alert Bot

This bot monitors the sea level at Copenhagen (Langelinie station) every **6 hours** and sends a **Telegram warning** when the water rises above **+60 cm** or drops below **−60 cm** (relative to the DVR90 datum).

You can also visit the **live dashboard** at:
👉 **https://howislivingin-prog.github.io/wasserstand**

---

## What it does

- **⚠️ Alert** — sent when the level crosses ±60 cm
- **✅ All-clear** — sent when the level returns to normal
- **📢 Forecast warning** — sent up to 12 hours in advance if the DMI storm surge forecast predicts a threshold crossing
- **📍 /update command** — type `/update` in the Telegram group at any time to get the current sea level and 24h forecast peak on demand

All alerts include the current level, status, and a 24h forecast peak where available.

---

## How it works

1. GitHub runs the script automatically every 6 hours (for free, using **GitHub Actions**).
2. The script fetches the latest sea level reading from the Danish Meteorological Institute (DMI).
3. It also fetches the DMI storm surge forecast to check if a threshold breach is expected within the next 12 hours.
4. Messages are only sent when something changes — the bot does **not** spam you while the level stays above/below the threshold.
5. The bot's memory is stored in `state.json` and automatically saved back to this repository after each run.

---

## One-time setup (do this once)

You need two things before the bot can run:

| What | Where you get it |
|------|-----------------|
| **Telegram bot token** | Free from Telegram (see Step 1) |
| **Telegram chat ID** | Your chat/group ID (see Step 2) |

The water level data comes from the DMI Open Data API — **no account or API key needed**.

---

### Step 1 — Create a Telegram bot

1. Open Telegram and search for **@BotFather**.
2. Send the message `/newbot`.
3. When asked for a name, type something like `Copenhagen Water Alert`.
4. When asked for a username, type something like `cph_water_alert_bot` (must end in `bot`).
5. BotFather will reply with a **token** that looks like `123456789:ABCdef...` — copy it.

---

### Step 2 — Find your Telegram chat ID

**Option A — for a personal chat (just you):**

1. Search for **@userinfobot** in Telegram and start a chat with it.
2. It will immediately reply with your numeric **ID**, e.g. `123456789`.

**Option B — for a group chat:**

1. Add your new bot to the group.
2. Send any message in the group.
3. Open this URL in your browser (replace `TOKEN` with your bot token):
   ```
   https://api.telegram.org/botTOKEN/getUpdates
   ```
4. Look for `"chat":{"id":` in the response — the number after it (may be negative, e.g. `-987654321`) is your group chat ID.

---

### Step 3 — Add secrets to GitHub

Your Telegram credentials must never be stored directly in the code. GitHub lets you store them safely as **Secrets**.

1. Open your repository on GitHub: **https://github.com/howislivingin-prog/wasserstand**
2. Click **Settings** (the gear icon near the top right).
3. In the left sidebar click **Secrets and variables** → **Actions**.
4. Click **"New repository secret"** and add each of the following two secrets:

| Secret name | Value |
|-------------|-------|
| `TELEGRAM_BOT_TOKEN` | Your bot token from Step 1 |
| `TELEGRAM_CHAT_ID` | Your chat ID from Step 2 |

---

### Step 4 — Enable GitHub Actions

GitHub Actions should already be enabled on your repository. To confirm:

1. Click the **Actions** tab at the top of your repository.
2. If you see a message asking you to enable workflows, click **"I understand my workflows, go ahead and enable them"**.

---

### Step 5 — Test it manually

1. Go to the **Actions** tab on GitHub.
2. Click **"Water Level Check"** in the left sidebar.
3. Click **"Run workflow"** → **"Run workflow"** (green button).
4. Wait about 30 seconds, then click on the running job to see the output.

If everything is set up correctly you will see the current water level printed in the log. If the level is outside ±60 cm, you will receive a Telegram message immediately.

---

## Customising the threshold

The alert threshold is **±60 cm** by default. To change it:

1. Open `main.py` in this repository.
2. Find the line:
   ```python
   THRESHOLD_CM = 60
   ```
3. Change `60` to any number you want (e.g. `50` for ±50 cm).
4. Save and push the file — the next run will use the new value.
5. Also update the `THRESHOLD` value in `docs/index.html` to keep the website in sync.

---

## Files in this repository

| File | Purpose |
|------|---------|
| `main.py` | The main Python script |
| `requirements.txt` | Python libraries needed |
| `state.json` | Tracks the last known alert state (updated automatically) |
| `.github/workflows/water_level_check.yml` | Tells GitHub when and how to run the script |
| `docs/index.html` | The live dashboard website |

---

## Data source

Sea level data comes from the **Danish Meteorological Institute (DMI) Open Data API v2**.
Forecast data comes from the **DMI DKSS storm surge model**.
Station: **Copenhagen Langelinie** (station ID `30336`).
Values are in centimetres relative to the **DVR90** datum (Danish Vertical Reference 1990).
