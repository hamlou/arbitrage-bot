# Deployment (systemd)

`polymarket-bot.service` runs `python main.py` under systemd with automatic
restart on crash and a Telegram crash alert.

## Files

| File | Purpose |
| --- | --- |
| `polymarket-bot.service` | systemd unit: restart on crash, 10s backoff, 5-crash/300s restart cap, `ExecStopPost` crash hook |
| `send_crash_alert.py` | Hook invoked by `ExecStopPost`: sends a CRITICAL Telegram alert only on abnormal exits (never on a clean `systemctl stop`) |

## Install

1. **Edit the paths** in `polymarket-bot.service` — every `/opt/polymarket-arb-bot`
   must point at the real checkout (and the venv) on your server.
2. Copy and enable:

   ```bash
   sudo cp deploy/polymarket-bot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now polymarket-bot
   ```

3. Verify it started:

   ```bash
   systemctl status polymarket-bot
   journalctl -u polymarket-bot -f
   ```

## Restart behavior

- **Crash** (any non-zero exit): restarted after `RestartSec=10`.
- **Repeated crashes**: after 5 crashes within 300s systemd *stops*
  restarting (unit goes to `failed`) instead of hammering the Binance/
  Coinbase/Polymarket APIs once a second forever. The crash hook fires for
  the `start-limit-hit` stop on modern systemd — verify this once on your
  target version (`journalctl -u polymarket-bot` after a deliberate crash
  loop), since ExecStopPost-on-start-limit-hit has been version-dependent
  historically.
- **Non-root user (recommended)**: uncomment and set `User=` in the service
  file to run the bot as a dedicated user instead of root; that user needs
  read/write access to the checkout and its `.env`.
- **Clean stop** (`systemctl stop`, reboot): main.py converts SIGTERM into an
  orderly shutdown and exits 0 — no restart, no alert.

## Crash alerts

The bot's in-process alerts (alerts/telegram.py) cover *errors inside* the
trading loop, but a full process crash — an unhandled exception in
`setup()`, a dashboard task dying, an OOM kill — previously produced only a
traceback and exit, with no alert. The `ExecStopPost` hook closes that gap:
systemd runs it after the process dies and before restarting, and it pages
you with `$SERVICE_RESULT` / `$EXIT_CODE` / `$EXIT_STATUS` so you know
whether it was a crash, a signal, or systemd giving up.

## Manual test of the alert hook

```bash
# Simulate a crash (should send a CRITICAL Telegram alert):
SERVICE_RESULT=exit-code EXIT_CODE=exited EXIT_STATUS=1 \
  /opt/polymarket-arb-bot/.venv/bin/python deploy/send_crash_alert.py

# Simulate a clean stop (should stay silent, exit 0):
SERVICE_RESULT=success EXIT_CODE=ok EXIT_STATUS=0 \
  /opt/polymarket-arb-bot/.venv/bin/python deploy/send_crash_alert.py
```

The hook requires `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` (from `.env` or
environment), and the service's `WorkingDirectory` must be the repo root so
`.env` is found.

---

# Deployment (Render free tier — no credit card)

The only no-credit-card cloud that can run this bot 24/7. Verified against
`docs.render.com/docs/free` (checked Aug 2026):

| Free-tier fact | Consequence |
| --- | --- |
| Spins down after 15 min **without inbound traffic** | keep it awake with a free ping (below) |
| 750 free instance-hours/month | one always-awake service uses ~720 — fits |
| Local disk **wiped** on every restart/redeploy/spin-down | the bot auto-backs its SQLite ledger to Telegram (`CLOUD_BACKUP_ENABLED`) and restores it on boot |
| No free background workers | the API and the bot run in ONE process (`cloud_entry.py`) |
| May suspend a service with uncommonly high *outbound* traffic | the bot's WS streams are modest, but if a deploy mysteriously dies, this is the first thing to suspect |

## Files

| File | Purpose |
| --- | --- |
| `../cloud_entry.py` | Runs the FastAPI Command Center (for health checks/pings) in a thread + the bot in the main thread |
| `render.yaml` | Render blueprint: free web service, env vars, health path |
| `../engine/cloud_backup.py` | Telegram snapshot/restore of `storage/arb_bot.db` (inert unless enabled) |

## Setup — 15 minutes, zero dollars

1. **Push this repo to GitHub** if you haven't already.
2. **Create a free Render account** at render.com → “Sign up” (no credit card).
3. **Deploy the blueprint:** Render dashboard → **New → Blueprint** → paste the
   repo URL → Render reads `deploy/render.yaml` and creates the `arb-bot`
   service. It builds (`pip install -r requirements.txt`) and starts
   (`python cloud_entry.py`) automatically.

   > **Region matters — this is not optional.** The bot streams Binance.com's
   > WebSocket, and Binance geo-blocks US IPs. A service in a US region
   > (Oregon/Virginia/Ohio) gets **zero Binance data** and never trades
   > (2026-08-09: the first deploy did exactly this — Polymarket healthy,
   > Binance dead). The blueprint pins `region: frankfurt`; if you ever
   > recreate the service manually, pick **Frankfurt** (or Singapore), never
   > a US region. Render cannot change an existing service's region — you
   > must delete and recreate.
4. **Set the secrets** (service → Environment):
   - `TELEGRAM_BOT_TOKEN` — your existing bot token (same one as `.env`).
   - `TELEGRAM_CHAT_ID` — your existing chat id.
   - `PAPER_MODE=true` is already set by the blueprint; never add a private key.
5. **Keep it awake:** create a free account at cron-job.org (or UptimeRobot)
   → new job → URL `https://<your-service>.onrender.com/api/health` → every
   5 minutes. This is what your “ping every 14 min” idea does; 5 min is safer
   than 14 against the 15-min limit.
6. **First boot:** the disk is empty and there is no backup yet, so the bot
   starts a fresh $1,000 paper account and begins trading immediately.

## Restart / disk-wipe behavior

Render restarts free services “at any time”, and each restart wipes the disk.
The bot handles it:

1. Every 15 min it sends a consistent snapshot of the ledger to your Telegram
   chat as `arb_bot_backup.db` — you always hold a copy you can see.
2. On boot with a wiped DB it posts **“Cloud restart detected — forward the
   latest arb_bot_backup.db”** and waits 2 minutes. Forward the latest backup
   document back to the bot → it validates and restores the ledger, then
   resumes exactly where it left off. Reply `fresh` (or do nothing for 2 min)
   to start a new $1,000 paper run instead.

Why forwarding? A bot can never read its own sent messages, so this small
manual step is the only way it can recover its own backup. It happens only
after a restart that wiped the disk (rare), and takes 5 seconds.

## Watching it

- `https://<your-service>.onrender.com/api/health` — shows `bot_status`:
  `online` (state file fresh) vs `offline` (bot down).
- `https://<your-service>.onrender.com/api/overview` and `/api/trades` — the
  full Command Center readouts, served from the live ledger.
- The existing Telegram status digests (`/status`, `/stats`) work exactly as
  they do locally.
