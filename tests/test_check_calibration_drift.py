"""
Tests for scripts/check_calibration_drift.py — the report-only check that
compares recent paper-trade outcomes against the model's recorded predicted
probabilities using the same quantile binning as calibrate_momentum_model.py.

Every test uses a temporary SQLite database with hand-placed signal
timestamps / implied probabilities and trade outcomes — no network, no real
calibration file, and nothing is ever refit or written to config/calibration.json.
"""
import sqlite3
import time

import scripts.check_calibration_drift as check
from storage.db import Database

BASE_TS = time.time() - 100.0  # inside the 7-day window at test time


def _insert_signal(conn, market_id, ts, implied_prob):
    conn.execute(
        "INSERT INTO signals (ts, market_id, asset, implied_prob, polymarket_prob,"
        " edge_pct, confidence, fired) VALUES (?,?,?,?,?,?,?,?)",
        (ts, market_id, "BTC", implied_prob, 0.5, 1.0, 0.9, 1),
    )


def _insert_trade(conn, market_id, entry_ts, side, won, strategy="latency_arb",
                  mode="PAPER", exit_reason="SETTLED", realized=None):
    conn.execute(
        "INSERT INTO trades (signal_id, market_id, asset, side, mode, strategy,"
        " entry_ts, entry_price, size_usd, fee_usd, exit_ts, exit_price,"
        " exit_reason, realized_pnl_usd, status)"
        " VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?, 'CLOSED')",
        (market_id, "BTC", side, mode, strategy, entry_ts, 0.5, 10.0, 0.1,
         entry_ts + 60.0, 1.0 if won else 0.0, exit_reason,
         5.0 if won else -9.0 if realized is None else realized),
    )


# ---------------------------------------------------------------- helpers


async def _build_calibrated(db_path, n_per_bin=10):
    """Two bins: predicted 0.60 (60% wins) and predicted 0.80 (80% wins)."""
    conn = sqlite3.connect(db_path)
    ts = BASE_TS
    for i in range(n_per_bin):
        _insert_signal(conn, f"m60_{i}", ts, 0.60)
        _insert_trade(conn, f"m60_{i}", ts + 1.0, "YES", won=(i < int(n_per_bin * 0.6)))
        ts += 5.0
    for i in range(n_per_bin):
        _insert_signal(conn, f"m80_{i}", ts, 0.80)
        _insert_trade(conn, f"m80_{i}", ts + 1.0, "YES", won=(i < int(n_per_bin * 0.8)))
        ts += 5.0
    conn.commit()
    conn.close()


# ------------------------------------------------------------------ tests


async def test_calibrated_outcomes_report_match(tmp_path, capsys):
    db_path = str(tmp_path / "ok.db")
    db = Database(db_path)
    await db.connect()
    await db.close()
    await _build_calibrated(db_path, n_per_bin=10)  # 20 trades, bins match

    rc = await check.check(db_path, days=7.0, n_bins=2, tolerance=0.10, min_bin_n=5)
    out = capsys.readouterr().out
    assert rc == 0
    assert "CALIBRATION DRIFT CHECK" in out
    assert "OUTCOMES STILL MATCH PREDICTIONS" in out
    assert "REPORT ONLY" in out
    # Both bins should show a near-zero gap (actual - predicted).
    assert "20" in out  # 20 directional trades matched


async def test_drifted_predictions_report_drift(tmp_path, capsys):
    """Predicted 0.90 but only 40% actually win — must flag overconfidence."""
    db_path = str(tmp_path / "drift.db")
    db = Database(db_path)
    await db.connect()
    await db.close()

    conn = sqlite3.connect(db_path)
    ts = BASE_TS
    for i in range(10):
        _insert_signal(conn, f"m90_{i}", ts, 0.90)
        _insert_trade(conn, f"m90_{i}", ts + 1.0, "YES", won=(i < 4))  # 4/10 wins
        ts += 5.0
    conn.commit()
    conn.close()

    rc = await check.check(db_path, days=7.0, n_bins=1, tolerance=0.10, min_bin_n=5)
    out = capsys.readouterr().out
    assert rc == 1
    assert "RESULT: DRIFTED" in out
    assert "overconfident" in out
    # actual ~0.40 vs predicted ~0.90 -> gap magnitude clearly reported
    assert "0.40" in out or "0.500" in out or "0.5" in out


async def test_no_trades_reports_nothing_to_compare(tmp_path, capsys):
    db_path = str(tmp_path / "empty.db")
    db = Database(db_path)
    await db.connect()
    await db.close()

    rc = await check.check(db_path, days=7.0)
    out = capsys.readouterr().out
    assert rc == 0
    assert "No completed directional paper trades" in out


async def test_no_side_prediction_is_one_minus_implied(tmp_path, capsys):
    """A NO-side trade with signal implied_prob=0.40 should be scored as
    predicted win prob = 0.60."""
    db_path = str(tmp_path / "no_side.db")
    db = Database(db_path)
    await db.connect()
    await db.close()

    conn = sqlite3.connect(db_path)
    ts = BASE_TS
    for i in range(10):
        _insert_signal(conn, f"no_{i}", ts, 0.40)
        _insert_trade(conn, f"no_{i}", ts + 1.0, "NO", won=(i < 6))  # 60% win
        ts += 5.0
    conn.commit()
    conn.close()

    rc = await check.check(db_path, days=7.0, n_bins=1, tolerance=0.10, min_bin_n=5)
    out = capsys.readouterr().out
    assert rc == 0
    # mean predicted shown should be ~0.60, actual ~0.60 -> match
    assert "0.600" in out


async def test_old_trades_excluded_from_window(tmp_path, capsys):
    """Trades entered before the window must be ignored, not compared."""
    db_path = str(tmp_path / "old.db")
    db = Database(db_path)
    await db.connect()
    await db.close()

    conn = sqlite3.connect(db_path)
    _insert_signal(conn, "m_old", BASE_TS - 10 * 86400, 0.80)
    _insert_trade(conn, "m_old", BASE_TS - 10 * 86400 + 1.0, "YES", won=True)
    _insert_signal(conn, "m_new", BASE_TS, 0.60)
    _insert_trade(conn, "m_new", BASE_TS + 1.0, "YES", won=True)
    conn.commit()
    conn.close()

    # min_bin_n=5 so the single matched trade isn't judged as a drifted bin
    # (its actual 1.00 vs predicted 0.60 would legitimately exceed tolerance).
    rc = await check.check(db_path, days=7.0, n_bins=1, tolerance=0.10, min_bin_n=5)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Closed paper trades     : 1" in out  # the 10-day-old trade is excluded
    assert "0 sum-to-one excluded" in out
    # Only the new trade participates -> 1 directional matched.
    assert "Directional, signal matched: 1" in out


async def test_sum_to_one_trades_excluded(tmp_path, capsys):
    db_path = str(tmp_path / "sto.db")
    db = Database(db_path)
    await db.connect()
    await db.close()

    conn = sqlite3.connect(db_path)
    _insert_signal(conn, "m_arb", BASE_TS, 0.60)
    _insert_trade(conn, "m_arb", BASE_TS + 1.0, "YES", won=True, strategy="sum_to_one")
    _insert_signal(conn, "m_dir", BASE_TS + 100.0, 0.60)
    _insert_trade(conn, "m_dir", BASE_TS + 101.0, "YES", won=True)
    conn.commit()
    conn.close()

    rc = await check.check(db_path, days=7.0, n_bins=1, tolerance=0.10, min_bin_n=5)
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 sum-to-one excluded" in out
    assert "Directional, signal matched: 1" in out


async def test_trade_without_signal_is_counted_not_compared(tmp_path, capsys):
    db_path = str(tmp_path / "nosig.db")
    db = Database(db_path)
    await db.connect()
    await db.close()

    conn = sqlite3.connect(db_path)
    _insert_trade(conn, "m_nosig", BASE_TS, "YES", won=True)  # no signal row
    _insert_signal(conn, "m_ok", BASE_TS + 200.0, 0.60)
    _insert_trade(conn, "m_ok", BASE_TS + 201.0, "YES", won=True)
    conn.commit()
    conn.close()

    rc = await check.check(db_path, days=7.0, n_bins=1, tolerance=0.10, min_bin_n=5)
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 had no logged signal" in out
    assert "Directional, signal matched: 1" in out


async def test_report_only_never_refits_or_writes_model(tmp_path, monkeypatch, capsys):
    """The script must never call save_calibration or touch the live file."""
    db_path = str(tmp_path / "safe.db")
    db = Database(db_path)
    await db.connect()
    await db.close()
    await _build_calibrated(db_path, n_per_bin=10)

    live_path = tmp_path / "live_calibration.json"
    monkeypatch.setattr("engine.calibration.DEFAULT_CALIBRATION_PATH", str(live_path))

    calls = []

    def _boom(*a, **k):
        calls.append(a)
        raise AssertionError("check_calibration_drift.py must never save a calibration")

    monkeypatch.setattr("engine.calibration.save_calibration", _boom)

    rc = await check.check(db_path, days=7.0, n_bins=2, tolerance=0.10, min_bin_n=5)
    out = capsys.readouterr().out
    assert rc == 0
    assert calls == []            # save_calibration never invoked
    assert not live_path.exists()  # nothing written to the live model path
    assert "REPORT ONLY" in out


async def test_uses_real_quantile_bin_stats_and_fit(tmp_path, monkeypatch, capsys):
    """The drift check must call the real engine.calibration routines — the
    same binning as calibrate_momentum_model.py — not a reimplementation."""
    db_path = str(tmp_path / "spy.db")
    db = Database(db_path)
    await db.connect()
    await db.close()
    await _build_calibrated(db_path, n_per_bin=10)

    from engine.calibration import quantile_bin_stats as real_bin
    from engine.calibration import fit_calibration as real_fit

    bin_calls, fit_calls = [], []

    def _bin_spy(values, outcomes, n_bins):
        bin_calls.append(n_bins)
        return real_bin(values, outcomes, n_bins)

    def _fit_spy(samples, horizon_minutes, n_bins=8):
        fit_calls.append((horizon_minutes, n_bins))
        return real_fit(samples, horizon_minutes=horizon_minutes, n_bins=n_bins)

    monkeypatch.setattr("scripts.check_calibration_drift.quantile_bin_stats", _bin_spy)
    monkeypatch.setattr("scripts.check_calibration_drift.fit_calibration", _fit_spy)

    rc = await check.check(db_path, days=7.0, n_bins=2, tolerance=0.10, min_bin_n=5)
    out = capsys.readouterr().out
    assert rc == 0
    assert bin_calls == [2]          # binning requested with the configured n_bins
    assert fit_calls == [(15, 2)]    # same-method curve fit with default horizon label
    assert "Same-method fitted curve" in out
