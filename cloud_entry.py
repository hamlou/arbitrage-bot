"""
Render (or any cloud host) entrypoint.

Runs TWO things in one process, because Render's free tier only supports free
*web* services (no free background workers):

  1. The Command Center API (FastAPI, command_center/api/main.py) — in a
     daemon thread. This is what Render's health check and keep-alive pings
     hit, so the free instance never spins down.
  2. The bot itself (main.py) — in the main thread, exactly as it runs
     locally (same single-instance lock, same Telegram loop, same loops).

On SIGTERM (Render shutdown), the bot's own shutdown handling unwinds
``main()`` and the process exits; the API thread is a daemon and dies with it.
"""
from __future__ import annotations

import asyncio
import os
import threading


def _run_api() -> None:
    import uvicorn

    from command_center.api.main import app

    port = int(os.environ.get("PORT", "10000"))
    # uvicorn.run() in a non-main thread skips signal-handler installation,
    # which is exactly what we want here — the bot owns shutdown.
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


def main() -> None:
    if os.environ.get("CLOUD_ENTRY_API_ONLY") == "1":
        _run_api()
        return
    threading.Thread(target=_run_api, daemon=True).start()

    from main import main as bot_main

    try:
        asyncio.run(bot_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
