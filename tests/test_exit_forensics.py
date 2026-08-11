"""
Tests for the exit-forensics digest (engine/exit_forensics.py + the
alerts/status_report.py formatter). This is the freeze-compliant reporting
layer that closes the loop on the freeze rule: the premature-vs-protective
EDGE_REVERSAL split and both sample-size gates are pushed to Telegram
automatically, without touching any threshold.
"""
from __future__ import annotations

from alerts.status_report import format_forensics_digest
from engine.exit_forensics import build_digest_summary, classify_early_exits


def _trade(tid, exit_reason, pnl, entry=0.50, exit_px=0.45, ts=1700000000.0, status="CLOSED"):
    return {
        "id": tid, "status": status, "exit_reason": exit_reason,
        "realized_pnl_usd": pnl, "entry_price": entry, "exit_price": exit_px,
        "entry_ts": ts,
    }


def _probe(trade_id, label, quote_price, ts):
    return {
        "trade_id": trade_id, "sample_label": label, "quote_price": quote_price,
        "entry_price": 0.50, "ts": ts,
    }


def test_classify_early_exits_buckets_by_post_exit_recovery():
    """The core classification: a reversal is PREMATURE if the market hit the
    reprice target after we left; protective if it kept falling."""
    trades = [
        _trade(1, "EDGE_REVERSAL", -10.0),   # market recovered -> premature
        _trade(2, "EDGE_REVERSAL", -8.0),    # kept falling -> protective
        _trade(3, "EDGE_REVERSAL", -5.0),    # no probes -> no_data
        _trade(4, "REPRICE", +12.0),         # not a reversal, ignored here
    ]
    probes = [
        # trade 1: after exit, price rose from 0.50 entry to 0.60 = +20% >= 10%
        _probe(1, "P_30S", 0.60, ts=2.0),
        _probe(1, "P_SETTLED", 0.10, ts=3.0),   # held side lost, but recovery already happened
        # trade 2: never recovered
        _probe(2, "P_30S", 0.40, ts=2.0),
        _probe(2, "P_SETTLED", 0.05, ts=3.0),
    ]
    cls = classify_early_exits(trades, probes, reprice_target=0.10)
    assert [t["id"] for t, _, _ in cls["premature"]] == [1]
    assert [t["id"] for t, _, _ in cls["protective"]] == [2]
    assert [t["id"] for t in cls["no_data"]] == [3]
    assert len(cls["reversals"]) == 3  # trade 4 (REPRICE) not counted


def test_classify_early_exits_held_side_won_bucket():
    """Held-side-won: no post-exit reprice above target, but the held token
    settled at $1 — the exit cut a guaranteed payout."""
    trades = [_trade(1, "EDGE_REVERSAL", -7.0)]
    probes = [
        _probe(1, "P_120S", 0.54, ts=2.0),      # +8% recovery, under the +10% target
        _probe(1, "P_SETTLED", 1.00, ts=3.0),  # 0.50 -> 1.00 = won
    ]
    cls = classify_early_exits(trades, probes, reprice_target=0.10)
    assert [t["id"] for t, _, _ in cls["held_won"]] == [1]
    assert cls["premature"] == []
    assert cls["protective"] == []


def test_build_digest_summary_counts_and_gates():
    summary = build_digest_summary(
        all_trades=[
            _trade(1, "EDGE_REVERSAL", -10.0, ts=1700000000.0),
            _trade(2, "EDGE_REVERSAL", -8.0, ts=1700000000.0),
            _trade(3, "REPRICE", +12.0, ts=1700000000.0),
        ],
        probes=[
            _probe(1, "P_30S", 0.60, ts=2.0),   # premature
            _probe(2, "P_30S", 0.40, ts=2.0),   # protective
        ],
        reprice_target=0.10,
        freeze_min_trades=100, freeze_min_days=7.0,
        live_min_trades=200, live_min_days=7.0, live_min_distinct_days=5,
    )
    assert summary["closed_trades"] == 3
    assert summary["reversals_n"] == 2
    assert summary["premature_n"] == 1
    assert summary["protective_n"] == 1
    assert summary["no_data_n"] == 0
    assert summary["premature_dollars"] == -10.0
    assert summary["by_reason"]["REPRICE"] == 12.0
    # Gates surfaced: freeze (100) is distinct from live (200).
    assert summary["freeze_min_trades"] == 100
    assert summary["live_min_trades"] == 200


def test_format_forensics_digest_renders_all_sections():
    summary = build_digest_summary(
        all_trades=[
            _trade(1, "EDGE_REVERSAL", -10.0, ts=1700000000.0),
            _trade(2, "REPRICE", +12.0, ts=1700000000.0),
        ],
        probes=[_probe(1, "P_30S", 0.60, ts=2.0)],
        reprice_target=0.10,
        freeze_min_trades=100, freeze_min_days=7.0,
        live_min_trades=200, live_min_days=7.0, live_min_distinct_days=5,
    )
    text = format_forensics_digest(summary)
    assert "EXIT FORENSICS" in text
    assert "Closed trades" in text and "2" in text
    assert "PREMATURE" in text
    assert "1 trades" in text
    assert "Freeze gate" in text and "100" in text
    assert "Live-trading gate" in text and "200" in text
    assert "Measurement only" in text


def test_format_forensics_digest_handles_empty_run():
    text = format_forensics_digest({
        "closed_trades": 0, "days_elapsed": 0.0, "distinct_trading_days": 0,
        "net_pnl_usd": 0.0, "by_reason": {},
        "reversals_n": 0, "premature_n": 0, "premature_dollars": 0.0,
        "reversal_dollars": 0.0, "held_won_n": 0, "protective_n": 0,
        "no_data_n": 0,
        "freeze_min_trades": 100, "freeze_min_days": 7.0,
        "live_min_trades": 200, "live_min_days": 7.0, "live_min_distinct_days": 5,
    })
    assert "EXIT FORENSICS" in text
    assert "Freeze gate" in text
