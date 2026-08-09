"""
Tests for scripts/backtest.py — the historical strategy backtest.

No real network anywhere: price data and (optionally) order-book snapshots are
written to temp CSVs, and the script is exercised end-to-end through its own
main().
"""
import csv
import random
import sys
from pathlib import Path

import scripts.backtest as backtest


def _write_price_csv(
    path: Path,
    n: int = 400,
    dt: float = 1.0,
    start: float = 1_700_000_000.0,
    seed: int = 42,
    drift: float = 0.0,
) -> None:
    """Deterministic 1-second-spaced BTC series with small noise. A positive
    `drift` gives the series a real directional move (the live strategy's
    fresh-move gate requires aligned recent momentum before firing — a pure
    random walk with ~0.05% 15s moves sits below that threshold and would
    honestly produce no trades)."""
    rng = random.Random(seed)
    price = 65_000.0
    rows = []
    for i in range(n):
        rows.append((start + i * dt, price))
        price *= 1 + drift + 0.0005 * (rng.random() - 0.5)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "price"])
        w.writerows(rows)


def _run(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["backtest", *args])
    return backtest.main()


def _line_with(out: str, label: str) -> str:
    for line in out.splitlines():
        if label in line:
            return line
    return ""


# -- Binance-only mode -------------------------------------------------------


def test_binance_only_prints_exact_warning_and_evaluates_windows(tmp_path, monkeypatch, capsys):
    price_csv = tmp_path / "btc_1s.csv"
    _write_price_csv(price_csv)

    code = _run(monkeypatch, "--price-csv", str(price_csv), "--asset", "BTC",
                "--horizon-minutes", "5", "--decision-lag-s", "15.0")
    out, err = capsys.readouterr()

    assert code == 0
    # The exact required warning is the very first thing printed.
    assert out.startswith(backtest.WARNING_NO_ORDERBOOK)
    line = _line_with(out, "Windows evaluated")
    assert line
    assert int(line.split(":")[-1].strip()) > 0


def test_accepts_binance_klines_format(tmp_path, monkeypatch, capsys):
    """Same 12-column data.binance.vision layout the calibrate script accepts."""
    klines = tmp_path / "klines.csv"
    with klines.open("w") as f:
        for i in range(400):
            f.write(f"{int((1_700_000_000 + i) * 1000)},65000,65010,64990,65005,10,"
                    f"{i},1000,5,5,500,0\n")

    code = _run(monkeypatch, "--binance-klines-csv", str(klines),
                "--asset", "BTC", "--horizon-minutes", "5")
    out, err = capsys.readouterr()

    assert code == 0
    assert "400 points" in _line_with(out, "Price data")


def test_klines_loader_handles_microsecond_timestamps(tmp_path):
    """data.binance.vision 1s klines ship open_time in MICROSECONDS (~1.78e15
    for 2026). The loader must convert to seconds, not treat them as
    milliseconds — that bug produced a "4000-day span" and coin-flip model
    output on real July-2026 data (found 2026-08-09)."""
    from scripts.calibrate_momentum_model import load_binance_klines_csv

    klines = tmp_path / "klines_1s.csv"
    with klines.open("w") as f:
        for i in range(5):
            us = 1_782_864_000_000_000 + i * 1_000_000  # 2026-07-01 00:00:00Z, 1s apart
            f.write(f"{us},65000,65010,64990,65005,10,{i},1000,5,5,500,0\n")

    timestamps, prices = load_binance_klines_csv(klines)
    assert timestamps[0] == 1_782_864_000.0  # seconds, not ms
    assert timestamps[1] - timestamps[0] == 1.0
    assert prices[0] == 65005.0


def test_klines_loader_still_accepts_millisecond_timestamps(tmp_path):
    """The klines API and most archives use milliseconds — must keep working."""
    from scripts.calibrate_momentum_model import load_binance_klines_csv

    klines = tmp_path / "klines_ms.csv"
    with klines.open("w") as f:
        for i in range(3):
            ms = (1_782_864_000 + i) * 1000
            f.write(f"{ms},65000,65010,64990,65005,10,{i},1000,5,5,500,0\n")

    timestamps, _ = load_binance_klines_csv(klines)
    assert timestamps[0] == 1_782_864_000.0
    assert timestamps[1] - timestamps[0] == 1.0


def test_calls_real_fair_value_function(tmp_path, monkeypatch, capsys):
    """Proves the script drives the REAL engine/fair_value function rather
    than a reimplementation (the spy wraps the imported real function)."""
    price_csv = tmp_path / "btc_1s.csv"
    _write_price_csv(price_csv)
    calls = {"n": 0}
    real = backtest.fair_value_probability

    def spy(inputs):
        calls["n"] += 1
        return real(inputs)

    monkeypatch.setattr(backtest, "fair_value_probability", spy)

    code = _run(monkeypatch, "--price-csv", str(price_csv),
                "--horizon-minutes", "5", "--decision-lag-s", "15.0")
    out, err = capsys.readouterr()

    assert code == 0
    assert calls["n"] > 0


# -- Order-book mode (real snapshots supplied) --------------------------------


def test_orderbook_mode_simulates_fills_via_real_broker_walk(tmp_path, monkeypatch, capsys):
    price_csv = tmp_path / "btc_1s.csv"
    # Upward drift so the live fresh-move gate (aligned momentum required to
    # fire) passes deterministically at every decision point — the model leans
    # YES against a book at YES 0.03/0.04, and the drift aligns with that.
    _write_price_csv(price_csv, n=1100, drift=0.0003)  # ~18 min of 1s data
    timestamps, _ = backtest.load_price_csv(price_csv)  # reuse the real loader

    # Real-format order-book snapshots for one contract window in EACH THIRD of
    # the data span, priced far from fair value so the signal engine fires.
    ob_csv = tmp_path / "books.csv"
    rows = []
    for i in (30, 400, 750):  # one window per time period (750 keeps t_end within data)
        t_open = timestamps[i]
        ts = t_open + 10.0
        label = f"BTC-5m-{int(t_open)}"
        for token, bid_px, ask_px in (("yes", 0.03, 0.04), ("no", 0.95, 0.96)):
            rows.append([ts, label, token, "ask", ask_px, 10_000_000.0])
            rows.append([ts, label, token, "bid", bid_px, 10_000_000.0])
    with ob_csv.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "market", "token", "side", "price", "size"])
        w.writerows(rows)

    # Prove fills go through the REAL broker_paper walk (spy delegates to it).
    walk_calls = {"n": 0}
    real_walk = backtest.PaperBroker._walk_book_for_fill

    def spy(self, book, size_usd):
        walk_calls["n"] += 1
        return real_walk(self, book, size_usd)

    monkeypatch.setattr(backtest.PaperBroker, "_walk_book_for_fill", spy)

    code = _run(monkeypatch,
                "--price-csv", str(price_csv),
                "--orderbook-csv", str(ob_csv),
                "--horizon-minutes", "5", "--decision-lag-s", "15.0",
                "--min-liquidity-usd", "100",
                "--position-pct", "0.5",
                "--fee-pct", "0.0",
                "--starting-balance-usd", "1000.0")
    out, err = capsys.readouterr()

    assert code == 0
    # Real books were supplied, so the no-orderbook warning must NOT appear.
    assert backtest.WARNING_NO_ORDERBOOK not in out
    line = _line_with(out, "Trades executed")
    assert line
    assert int(line.split(":")[-1].strip()) == 3
    assert walk_calls["n"] == 3  # fills used the real broker_paper walk
    # The report MUST break results into THIRDS of the data span, with a
    # per-period win rate / expectancy / max drawdown line for every period.
    assert "PERFORMANCE BY TIME PERIOD" in out
    period_lines = [ln for ln in out.splitlines() if "/3" in ln and " trades " in ln]
    assert len(period_lines) == 3, period_lines
    for ln in period_lines:
        assert "win " in ln and "expectancy" in ln and "max drawdown" in ln
        assert "no trades" not in ln
    # NOTE: this runs with the DEFAULT --min-confidence (0.85). Signals only
    # fire because each window's decision tick is anchored to wall-clock now —
    # a regression in that anchoring (stale ticks -> confidence <= 0.67) would
    # make this test fail with zero trades.


# -- Walk-forward mode ---------------------------------------------------------


def test_walk_forward_fits_before_each_test_period_and_reports(tmp_path, monkeypatch, capsys):
    """
    --walk-forward splits the data into folds, fits the calibration curve on
    data STRICTLY before each test period (proven by spying on the sample
    builder), tests the next period without re-fitting, and prints the
    per-fold + pooled out-of-sample report with a verdict.
    """
    price_csv = tmp_path / "btc_1s.csv"
    _write_price_csv(price_csv, n=3000)  # enough span for several 5-min folds
    timestamps, _ = backtest.load_price_csv(price_csv)

    # Spy on the REAL sample builder: record every training-slice timestamp
    # range it is called with, so we can prove no test-period data leaks in.
    train_windows = []
    real_build = backtest.build_samples_from_price_series

    def spy_build(ts, px, **kw):
        train_windows.append((min(ts), max(ts)) if ts else None)
        return real_build(ts, px, **kw)

    monkeypatch.setattr(backtest, "build_samples_from_price_series", spy_build)

    code = _run(monkeypatch, "--price-csv", str(price_csv),
                "--horizon-minutes", "5", "--decision-lag-s", "15.0",
                "--walk-forward", "--walk-forward-folds", "4",
                "--lookback-s", "30", "--n-bins", "8")
    out, err = capsys.readouterr()

    assert code == 0
    assert "WALK-FORWARD BACKTEST" in out
    assert "PER-FOLD" in out
    assert "POOLED OUT-OF-SAMPLE" in out
    assert "IN-SAMPLE REFERENCE" in out
    assert "VERDICT" in out

    # 4 folds -> 3 tested periods (period 1 is training-only). Sample-builder
    # calls: 3 per-fold fits + 1 full-series in-sample reference fit.
    t0, t_last = timestamps[0], timestamps[-1]
    fold_dur = (t_last - t0) / 4.0
    assert len(train_windows) == 4, train_windows
    for k in range(1, 4):
        test_start = t0 + k * fold_dur
        tr_min, tr_max = train_windows[k - 1]
        # The k-th fit must never see data at-or-after its test fold start.
        assert tr_max < test_start, f"fold {k} trained on test-period data: {tr_max} >= {test_start}"
    # The 4th call is the in-sample reference over the FULL series.
    assert train_windows[3] == (t0, t_last)


def test_walk_forward_uses_calibrated_and_fair_value_models(tmp_path, monkeypatch, capsys):
    """Both models are scored per fold: the calibrated momentum model and the
    parameter-free fair-value baseline, with the pooled OOS block showing n>0."""
    price_csv = tmp_path / "btc_1s.csv"
    _write_price_csv(price_csv, n=3000)

    code = _run(monkeypatch, "--price-csv", str(price_csv),
                "--horizon-minutes", "5", "--decision-lag-s", "15.0",
                "--walk-forward", "--walk-forward-folds", "4")
    out, err = capsys.readouterr()

    assert code == 0
    assert "Calibrated momentum model : n=" in out
    assert "Fair-value model baseline : n=" in out
    # The verdict line references the out-of-sample Brier.
    assert "VERDICT:" in out


def test_walk_forward_survives_too_little_training_data(tmp_path, monkeypatch, capsys):
    """With too few price points for a fold to fit the curve, the report says
    so plainly (fit=NO / no predictions) instead of crashing."""
    price_csv = tmp_path / "btc_1s.csv"
    _write_price_csv(price_csv, n=600)  # tiny: fold 2's training slice is < n_bins*5 samples

    code = _run(monkeypatch, "--price-csv", str(price_csv),
                "--horizon-minutes", "5", "--decision-lag-s", "15.0",
                "--walk-forward", "--walk-forward-folds", "4")
    out, err = capsys.readouterr()

    assert code == 0
    assert "WALK-FORWARD BACKTEST" in out
    # Either some folds fit and report numbers, or the report says no
    # predictions could be produced — never a crash.
    assert "VERDICT:" in out


def test_walk_forward_requires_at_least_two_folds(tmp_path, monkeypatch, capsys):
    price_csv = tmp_path / "btc_1s.csv"
    _write_price_csv(price_csv)

    code = _run(monkeypatch, "--price-csv", str(price_csv),
                "--horizon-minutes", "5", "--walk-forward", "--walk-forward-folds", "1")
    out, err = capsys.readouterr()

    assert code == 1
    assert "--walk-forward-folds must be at least 2" in out


# -- Verdict thresholds (direct unit tests) -----------------------------------


def _verdict_res(oos_brier, ins_brier, fv_brier, oos_n=100, ins_n=200):
    return {
        "oos_calib": {"n": oos_n, "brier": oos_brier},
        "insample_calib": {"n": ins_n, "brier": ins_brier},
        "oos_fv": {"n": oos_n, "brier": fv_brier},
    }


def test_verdict_holds_up_when_oos_matches_insample():
    v = backtest._walk_forward_verdict(_verdict_res(0.25, 0.245, 0.25))
    assert "HOLD UP" in v


def test_verdict_roughly_holds_for_mild_gap():
    v = backtest._walk_forward_verdict(_verdict_res(0.28, 0.24, 0.25))
    assert "ROUGHLY hold" in v


def test_verdict_does_not_hold_for_large_gap():
    v = backtest._walk_forward_verdict(_verdict_res(0.33, 0.24, 0.25))
    assert "do NOT hold up" in v


def test_verdict_no_oos_predictions():
    v = backtest._walk_forward_verdict(
        {"oos_calib": {"n": 0}, "insample_calib": {"n": 0}, "oos_fv": {"n": 0}}
    )
    assert "no out-of-sample calibrated predictions" in v


def test_verdict_survives_missing_fair_value_baseline():
    """Regression guard for the latent KeyError when fv has n=0: the verdict
    must not index fv['brier'] when no fair-value windows exist."""
    res = {
        "oos_calib": {"n": 50, "brier": 0.27},
        "insample_calib": {"n": 0},  # in-sample fit failed
        "oos_fv": {"n": 0},          # no fair-value windows either
    }
    v = backtest._walk_forward_verdict(res)
    assert "out-of-sample calibrated Brier 0.270" in v
    assert "no in-sample reference" in v


# -- Input validation ----------------------------------------------------------


def test_requires_a_price_input(monkeypatch, capsys):
    code = _run(monkeypatch)
    out, err = capsys.readouterr()
    assert code == 1
    assert "--price-csv" in out


def test_rejects_decision_lag_at_or_above_horizon(tmp_path, monkeypatch, capsys):
    price_csv = tmp_path / "btc_1s.csv"
    _write_price_csv(price_csv)

    code = _run(monkeypatch, "--price-csv", str(price_csv),
                "--horizon-minutes", "5", "--decision-lag-s", "300.0")
    out, err = capsys.readouterr()

    assert code == 1
    assert "--decision-lag-s" in out
