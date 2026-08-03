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
