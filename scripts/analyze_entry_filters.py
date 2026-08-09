"""Analyze entry/exit filters in DOLLARS: which changes reduce the LOSERS
without giving up the WINNERS?

Reconstructs one trade per distinct market from the signals table (fair-value
era, edge > 5pt) and simulates the round-trip PnL ($100 stake, entry at the
signal's poly price, exit at +10% reprice with 2c spread + price-dependent
taker fees, or exit at the last observed price when the reprice never comes).

Findings from the Aug 7-9 data (79 markets, 63W/16L, baseline $1,910):
  - entry-price cap <= 0.70 (live): removes $403 of $770 loss (52%) for
    $145 of $2,680 win (5%) -> +$259. The one clean lever: expensive
    entries have capped upside (+10% is small at 0.75) and open-ended
    downside (a reversal can fall 50c).
  - tightening to 0.60/0.55: marginal (+$14 / +$90) — diminishing.
  - "model has no opinion" (|imp-0.5| < 0.05) filter: DESTROYS value — the
    biggest winners are exactly the no-opinion entries at extreme market
    prices (e.g. entry 0.105 -> +$344).
  - stop-losses at any level (10-30%): DESTROY value — winners dip below
    entry before repricing; the stop sells them right before they win.

Re-run this after a live run with real fills to see if the pattern holds.
"""
import argparse
import sqlite3

SPREAD = 0.02
FEE_RATE = 0.07
SIZE = 100.0
WINDOW = 240.0
GAIN = 1.10


def fee_frac(p: float) -> float:
    return FEE_RATE * (1 - p)  # taker fee as fraction of notional spent


def load_markets(db_path: str, min_edge: float) -> dict[str, list]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT ts, market_id, implied_prob, polymarket_prob, edge_pct "
        "FROM signals WHERE reason LIKE '%fair_value%' AND edge_pct > ? ORDER BY ts",
        (min_edge,),
    ).fetchall()
    markets: dict[str, list] = {}
    for ts, mid, imp, poly, edge in rows:
        markets.setdefault(mid, []).append(
            {"ts": ts, "imp": imp, "poly": poly, "edge": edge}
        )
    for sigs in markets.values():
        sigs.sort(key=lambda s: s["ts"])
    return markets


def round_trip_pnl(sigs: list, stop: float | None) -> float:
    """Simulate one market's round-trip with an optional stop-loss."""
    e = sigs[0]
    ep = e["poly"]
    shares = SIZE / ep
    entry_fee = SIZE * fee_frac(ep)
    for l in sigs:
        if l["ts"] - e["ts"] > WINDOW:
            break
        if l["poly"] >= ep * GAIN:
            xp = min(l["poly"], 1.0) - SPREAD / 2
            pr = shares * xp
            return pr - pr * fee_frac(xp) - SIZE - entry_fee
        if stop is not None and l["poly"] <= ep * (1 - stop):
            xp = min(l["poly"], 1.0) - SPREAD / 2
            pr = shares * xp
            return pr - pr * fee_frac(xp) - SIZE - entry_fee
    xp = min(sigs[-1]["poly"], 1.0) - SPREAD / 2
    pr = shares * xp
    return pr - pr * fee_frac(xp) - SIZE - entry_fee


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="storage/arb_bot.db")
    ap.add_argument("--min-edge", type=float, default=0.05)
    args = ap.parse_args()

    markets = load_markets(args.db, args.min_edge)
    trades = [
        {"mid": mid, "sigs": sigs, "entry": sigs[0]["poly"], "imp": sigs[0]["imp"]}
        for mid, sigs in markets.items()
    ]
    baseline = sum(round_trip_pnl(t["sigs"], None) for t in trades)
    wins = [t for t in trades if round_trip_pnl(t["sigs"], None) > 0]
    losses = [t for t in trades if round_trip_pnl(t["sigs"], None) <= 0]
    loss_total = sum(round_trip_pnl(t["sigs"], None) for t in losses)
    win_total = sum(round_trip_pnl(t["sigs"], None) for t in wins)

    print(f"Markets: {len(trades)} | winners {len(wins)} ({len(wins)/len(trades):.1%}), "
          f"losers {len(losses)}. Baseline PnL ${baseline:,.0f} "
          f"(win ${win_total:,.0f}, loss ${loss_total:,.0f})")
    print(f"  entry > 0.70 losers: {sum(1 for t in losses if t['entry']>0.70)} "
          f"(${sum(round_trip_pnl(t['sigs'],None) for t in losses if t['entry']>0.70):,.0f} of ${loss_total:,.0f})")
    print(f"  entry > 0.70 winners: {sum(1 for t in wins if t['entry']>0.70)} "
          f"(${sum(round_trip_pnl(t['sigs'],None) for t in wins if t['entry']>0.70):,.0f} of ${win_total:,.0f})\n")

    def report(name: str, pred) -> None:
        kept = sum(round_trip_pnl(t["sigs"], None) for t in trades if not pred(t))
        bl = sum(1 for t in losses if pred(t))
        bw = sum(1 for t in wins if pred(t))
        print(f"  {name:<22} PnL ${kept:,.0f} (delta {kept-baseline:+,.0f})  "
              f"blocks {bl}/{len(losses)} losers, costs {bw}/{len(wins)} winners")

    print("=== entry filters ===")
    report("entry > 0.70 (live)", lambda t: t["entry"] > 0.70)
    report("entry > 0.60", lambda t: t["entry"] > 0.60)
    report("entry > 0.55", lambda t: t["entry"] > 0.55)
    report("|imp-0.5| < 0.05 (no-opinion)", lambda t: abs(t["imp"] - 0.5) < 0.05)

    print("\n=== exit stops (DESTROY value — winners dip first) ===")
    for stop in (0.10, 0.15, 0.20, 0.30):
        total = sum(round_trip_pnl(t["sigs"], stop) for t in trades)
        n_w = sum(1 for t in trades if round_trip_pnl(t["sigs"], stop) > 0)
        print(f"  stop {stop:.0%}: PnL ${total:,.0f} (delta {total-baseline:+,.0f}), wins {n_w}/{len(trades)}")

    print("\n=== losers surviving the live 0.70 cap ===")
    for t in sorted(losses, key=lambda t: round_trip_pnl(t["sigs"], None)):
        if t["entry"] > 0.70:
            continue
        print(f"  {t['mid']} entry={t['entry']:.3f} imp={t['imp']:.3f} "
              f"pnl=${round_trip_pnl(t['sigs'], None):,.0f}")


if __name__ == "__main__":
    main()
