"""
Pre-flight check for the 7-day validation run.

Refuses to say GO unless ALL of the following are true:
  1. `pytest -q` passes with zero failures on the current commit.
  2. System clock drift is under 50 ms (see scripts/check_clock_drift.py).
  3. Both feeds report healthy over a real ~30s live smoke test (Binance WS
     ticks arriving, Polymarket WS delivering fresh order books).
  4. PAPER_MODE=True and POLYGON_PRIVATE_KEY is unset in the current .env.
  5. The paper balance is at a clean, known starting value (fresh DB or a
     documented starting balance — NOT a leftover from a debug run).
  6. No other bot instance (main.py) is currently running.

Prints a clear GO / NO-GO with the specific failing check named.

Usage:
    python scripts/preflight_check.py
"""
from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

CLOCK_DRIFT_MAX_MS = 50.0
FEED_SMOKE_SECONDS = 30.0
BINANCE_TICKS_REQUIRED = 5
FRESH_DB_OK = object()  # sentinel: DB file doesn't exist yet -> genuinely fresh


# -- checks ------------------------------------------------------------------

def check_pytest() -> tuple[bool, str]:
    """Tests must pass on the current commit before the run starts."""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--no-header"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=900,
        )
    except subprocess.TimeoutExpired:
        return False, "pytest timed out after 900s"
    out = (r.stdout or "") + (r.stderr or "")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    last = lines[-1].strip() if lines else ""
    if r.returncode == 0 and "passed" in last and "failed" not in last:
        return True, last
    return False, (last[:300] if last else "pytest failed (no summary line)")


def check_clock_drift() -> tuple[bool, str]:
    try:
        from scripts.check_clock_drift import measure_drift_ms
    except Exception as exc:  # pragma: no cover - import sanity
        return False, f"could not import check_clock_drift: {exc}"
    try:
        drift_ms, source = measure_drift_ms(timeout=4.0)
    except Exception as exc:
        return False, f"drift measurement failed: {exc}"
    if drift_ms is None:
        return False, f"no time source reachable ({source})"
    ok = abs(drift_ms) < CLOCK_DRIFT_MAX_MS
    return ok, f"{drift_ms:+.1f} ms vs {source} (threshold <{CLOCK_DRIFT_MAX_MS:.0f} ms)"


async def _feed_smoke() -> tuple[bool, bool, dict]:
    """Live smoke test of both feeds, mirroring main.py's wiring."""
    from config.settings import settings as s
    from data.binance_feed import BinanceFeed
    from data.polymarket_feed import PolymarketFeed
    from data.polymarket_ws_feed import PolymarketWSFeed

    counters = {"binance_ticks": 0}
    binance_feed = BinanceFeed()
    feed = PolymarketFeed(min_liquidity_usd=s.MIN_MARKET_LIQUIDITY_USD)
    ws = PolymarketWSFeed(asset_ids=[])

    async def count_binance():
        try:
            async for _ in binance_feed.stream():
                counters["binance_ticks"] += 1
                if counters["binance_ticks"] >= BINANCE_TICKS_REQUIRED:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    b_task = asyncio.create_task(count_binance())
    ws_task = None
    token_ids: list[str] = []
    try:
        markets = await feed.discover_active_markets()
        for m in markets:
            token_ids.extend([m.token_id_yes, m.token_id_no])
        ws.update_assets(token_ids)
        ws_task = asyncio.create_task(ws.run())

        deadline = time.monotonic() + FEED_SMOKE_SECONDS
        while time.monotonic() < deadline:
            pm_ok = any(ws.is_fresh(tid) for tid in token_ids) if token_ids else False
            if counters["binance_ticks"] >= BINANCE_TICKS_REQUIRED and pm_ok:
                break
            await asyncio.sleep(0.5)
        pm_ok = any(ws.is_fresh(tid) for tid in token_ids) if token_ids else False
        binance_ok = counters["binance_ticks"] >= BINANCE_TICKS_REQUIRED
        return binance_ok, pm_ok, counters
    finally:
        b_task.cancel()
        if ws_task is not None:
            ws_task.cancel()
        await asyncio.gather(b_task, ws_task, return_exceptions=True)
        try:
            await feed.aclose()
        except Exception:
            pass


def check_feeds() -> tuple[bool, str]:
    try:
        binance_ok, pm_ok, counters = asyncio.run(_feed_smoke())
    except Exception as exc:
        return False, f"feed smoke test crashed: {exc}"
    bits = [
        f"binance {'OK' if binance_ok else 'NO'} ({counters['binance_ticks']} ticks in {FEED_SMOKE_SECONDS:.0f}s)",
        f"polymarket {'OK' if pm_ok else 'NO'} (fresh WS books present)",
    ]
    return (binance_ok and pm_ok), "; ".join(bits)


def check_paper_env() -> tuple[bool, str]:
    """PAPER_MODE=True and POLYGON_PRIVATE_KEY unset. Settings itself hard-
    crashes if a key is present in paper mode, so the import is the check."""
    try:
        from config.settings import settings
    except RuntimeError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"settings import failed: {exc}"
    if not settings.PAPER_MODE:
        return False, "PAPER_MODE is not True"
    if settings.POLYGON_PRIVATE_KEY:
        return False, "POLYGON_PRIVATE_KEY is set — never allowed in a paper run"
    # Belt-and-suspenders: check the .env file text for a populated key line.
    env = REPO_ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("POLYGON_PRIVATE_KEY=") and stripped[len("POLYGON_PRIVATE_KEY="):].strip() != "":
                return False, "POLYGON_PRIVATE_KEY has a value in .env"
    return True, "PAPER_MODE=True, POLYGON_PRIVATE_KEY unset"


def check_clean_balance() -> tuple[bool, str]:
    """Fresh DB (or zero trades) at the documented starting balance — never a
    leftover debug-run balance."""
    try:
        from config.settings import settings
        starting = settings.STARTING_PAPER_BALANCE_USD
        db_path = REPO_ROOT / settings.DATABASE_PATH
    except Exception:
        starting = 1000.0
        db_path = REPO_ROOT / "storage/arb_bot.db"
    if not db_path.exists():
        return True, f"fresh DB (no file yet) — clean start at ${starting:,.0f}"
    try:
        con = sqlite3.connect(str(db_path))
        cur = con.cursor()
        tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "trades" not in tables:
            con.close()
            return True, f"fresh DB (no trades table yet) — clean start at ${starting:,.0f}"
        n = cur.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        open_n = cur.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]
        pnl = cur.execute("SELECT COALESCE(SUM(realized_pnl_usd),0) FROM trades WHERE status='CLOSED'").fetchone()[0]
        con.close()
        if n == 0:
            return True, f"fresh DB, 0 trades — clean start at ${starting:,.0f}"
        return False, f"dirty DB: {n} trades ({open_n} open), closed PnL ${pnl:.2f} — archive it and start fresh"
    except Exception as exc:
        return False, f"could not inspect DB: {exc}"


def check_no_other_instance() -> tuple[bool, str]:
    """A second running bot = double trading. Refuse GO if one exists."""
    try:
        r = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
                "| Where-Object { $_.CommandLine -match 'main\\\\.py' } "
                "| Select-Object -ExpandProperty ProcessId",
            ],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        return False, f"could not enumerate processes: {exc}"
    pids = [p for p in r.stdout.split() if p.strip().isdigit()]
    if pids:
        return False, f"other bot instance(s) running: {', '.join(pids)} — stop them first"
    return True, "no other main.py process running"


CHECKS = [
    ("Tests (pytest -q)", check_pytest),
    ("Clock drift < 50ms", check_clock_drift),
    ("Feed health (30s live smoke)", check_feeds),
    ("PAPER_MODE / no private key", check_paper_env),
    ("Clean starting balance", check_clean_balance),
    ("No other bot instance", check_no_other_instance),
]


def main() -> int:
    print("=" * 62)
    print("VALIDATION RUN PRE-FLIGHT")
    print("=" * 62)
    results: list[tuple[bool, str]] = []
    for name, fn in CHECKS:
        ok, detail = fn()
        mark = "PASS" if ok else "FAIL"
        print(f"[ {mark} ] {name}")
        print(f"          {detail}")
        results.append((ok, name))
    print("-" * 62)
    all_ok = all(ok for ok, _ in results)
    if all_ok:
        print("RESULT: GO — the run can start. Config is frozen (see docs/VALIDATION_RUN_2026_08.md).")
    else:
        print("RESULT: NO-GO — fix the failing check(s) named above before starting the run.")
    print("=" * 62)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
