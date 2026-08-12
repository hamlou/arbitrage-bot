# NOTE: Polymarket has no sandbox/testnet. There is no faucet, no staging
# environment, no way to place a "fake" order against Polymarket's own infra.
# This PaperBroker is the closest equivalent: it simulates fills against the
# REAL live order book pulled from data/polymarket_feed.py, so slippage and
# spread are realistic, but the USDC balance and positions are entirely
# virtual — no wallet, no private key, no on-chain call anywhere in this file.
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from config.settings import settings
from data.polymarket_feed import Market, OrderBook, PolymarketFeed
from engine import fees
from engine.fees import DEFAULT_TAKER_FEE_RATE
from engine.sum_to_one import SumToOneOpportunity
from storage.db import Database

logger = logging.getLogger(__name__)

# Polymarket's crypto taker fee RATE (docs.polymarket.com/trading/fees) —
# applied as fee_rate * p * (1 - p) per share, NOT as a flat fraction of
# size (see engine/fees.py). Override per-instance for tests.
DEFAULT_FEE_PCT = DEFAULT_TAKER_FEE_RATE


@dataclass(frozen=True, slots=True)
class Fill:
    trade_id: int
    market_id: str
    side: str            # YES / NO
    avg_price: float
    size_usd: float
    shares: float
    fee_usd: float
    slippage_pct: float  # (avg_price - book_mid_at_decision) / book_mid_at_decision


@dataclass(slots=True)
class MakerOrder:
    """
    One resting sum-to-one maker order (added 2026-08-12): the CHEAPER leg
    of a sub-$1 YES+NO pair is posted as a resting BUY at the bid (maker =
    zero fee, and bid < ask so a strictly better entry price), and the
    instant it fills the OTHER leg is taken at market. This replaces the
    both-legs-taker flow's "cancel and reverse a half-open hedge" window —
    the exposure gap between the two legs shrinks to one book read.

    Lifecycle (in-memory; dies with the process like a real broker
    connection — the DB row is the audit trail only):
      PENDING -> FILLED (both legs opened as a combo) | REVERSED | CANCELLED
    """
    market: Market
    market_id: str
    maker_side: str       # YES / NO — the CHEAPER leg, resting at the bid
    taker_side: str       # YES / NO — the other leg, taken at market on fill
    maker_price: float    # resting limit price (= best bid at post)
    half_size_usd: float  # dollar budget per leg (informational)
    target_shares: float  # EQUAL share count per leg (equal-share sizing — the
                          # payout is then identical regardless of outcome)
    reserve_usd: float    # cash reserved at post (pair cost + est. taker fee)
    posted_at: float
    db_row_id: int
    status: str = "PENDING"  # PENDING / FILLED / REVERSED / CANCELLED / HELD


class InsufficientBalanceError(Exception):
    pass


class OrderTooSmallError(Exception):
    pass


class EntryPriceExceededError(Exception):
    """
    Raised by place_order when the ACTUAL walked fill price exceeds
    max_entry_price. The signal engine caps entry price at decision time
    (target_book.best_ask > MAX_DIRECTIONAL_ENTRY_PRICE), but the fill lands
    after the simulated fill latency against a book that keeps moving, and
    the walk pays the average across all consumed levels — on the thin books
    of 5-min crypto markets the ask can jump 0.20 -> 0.99 in the gap. Live
    2026-08-11: two directional entries decided at best_ask 0.20/0.49 filled
    at 0.99 (slippage 407%/104%) and settled at 0.0 — -$130 of losses from a
    price the decision-time gate never saw. Refuse the fill rather than open
    a position at a price the strategy never approved.
    """


class SumToOneEdgeLostError(Exception):
    """
    Raised by place_sum_to_one_order when the combined fill price of the two
    legs no longer locks a profit. Two distinct triggers: (1) the PRE-QUOTE
    guard — the real ask-walk of both legs (what the fills would actually
    pay) exceeds $1 even though the best asks summed below it, so nothing is
    opened (live 2026-08-09: best asks looked cheap but fills walked to
    0.86+0.15=1.01 — a guaranteed LOSS that the reversal then amplified to
    −$46.72); (2) the fills themselves land at or above $1 after the book
    moved during the fill latency (verified 2026-08-07: 0.31+0.73=1.04,
    0.17+0.89=1.06). In case (2) the broker reverses BOTH legs only when
    reversing loses less than holding to settlement (see
    _resolve_edge_loss), so a "guaranteed" pair is never held as a large
    guaranteed loss.
    """

    def __init__(self, combined_cost: float, locked_edge_pct: float, action: str = "both legs reversed"):
        super().__init__(
            f"Sum-to-one edge not locked: combined cost {combined_cost:.3f} "
            f"(locked edge {locked_edge_pct:.2%}) — {action}"
        )
        self.combined_cost = combined_cost
        self.locked_edge_pct = locked_edge_pct


def _round_to_tick(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return price
    return round(price / tick_size) * tick_size


class PaperBroker:
    """
    Virtual USDC balance + position ledger, with fills computed by walking the
    REAL live order book rather than filling at an idealized midpoint price.
    """

    def __init__(
        self,
        db: Database,
        feed: PolymarketFeed,
        starting_balance_usd: float,
        fee_pct: float = DEFAULT_FEE_PCT,
        simulated_fill_latency_s: float = 0.0,
        min_order_size_usd: float = 1.0,
        tick_size: float = 0.01,
    ):
        self.db = db
        self.feed = feed
        self.fee_pct = fee_pct
        self.balance_usd = starting_balance_usd
        # The ledger reconstruction in load_open_positions() must always start
        # from the SAME baseline, or a second call double-counts (it mutated
        # self.balance_usd on the first pass). Capture it once here.
        self._starting_balance_usd = starting_balance_usd
        self.simulated_fill_latency_s = simulated_fill_latency_s
        self.min_order_size_usd = min_order_size_usd
        self.tick_size = tick_size
        # market_id -> list of open trade_ids. Usually one; two for a
        # sum-to-one pair's simultaneous YES+NO legs. Source of truth is
        # always the DB — this is a cache, and load_open_positions() is how
        # it gets rebuilt after a restart.
        self._open_positions: dict[str, list[int]] = {}
        # market_id -> resting sum-to-one maker order (see MakerOrder).
        # In-memory only: a resting order dies with the process, exactly like
        # a real broker connection. The DB maker_orders rows are the audit
        # trail; load_open_positions() marks any stale PENDING rows CANCELLED
        # so a restart never leaves phantom "open" resting orders.
        self._maker_orders: dict[str, MakerOrder] = {}

    def _fee_rate_for(self, market: Market) -> float:
        """Category-aware taker fee RATE for a market (docs.polymarket.com/
        trading/fees, added 2026-08-12): geopolitics is fee-free, politics/
        finance 0.04, crypto 0.07. Falls back to the configured rate when the
        market's category is unknown. All fee charges flow through this so
        paper P&L matches what Polymarket would actually collect — a sum-to-
        one pair on a geopolitics market must not be charged the crypto rate.
        """
        if market.category:
            return fees.fee_rate_for_category(market.category)
        return self.fee_pct

    @property
    def mode(self) -> str:
        return "PAPER"

    async def get_balance(self) -> float:
        """Cash only — what's actually available to spend on a NEW order."""
        return self.balance_usd

    async def load_open_positions(self) -> int:
        """
        Restores in-memory open-position tracking from the DB. MUST be called
        once at startup (see main.py's setup()) — without this, any position
        still open from a previous run is invisible to settle_position() and
        get_equity(), since PaperBroker previously tracked open positions
        ONLY in memory and never reloaded them. Returns the count restored.

        Also restores the CASH BALANCE from the TRADE LEDGER. Balance is
        otherwise an in-memory float that resets to the starting balance on
        every restart, so a restart with open positions silently re-counted
        money that was already spent (the equity curve jumped UP ~$43 on a
        real 2026-08-06 restart). The equity curve is NOT used to restore:
        it was itself written by corrupted in-memory balances, so it carries
        the same bug forward. The ledger is the truth:

            balance = starting_balance
                      + sum(realized_pnl of CLOSED trades)   # payout - size - fee
                      - sum(size + fee of OPEN trades)        # already spent at entry
        """
        open_trades = await self.db.get_open_trades(mode=self.mode)
        self._open_positions.clear()
        for t in open_trades:
            self._open_positions.setdefault(t["market_id"], []).append(t["id"])

        # Reconstruct cash from the trade ledger.
        try:
            all_trades = await self.db.get_all_trades(mode=self.mode)
            restored_balance = self._starting_balance_usd  # fixed baseline, idempotent
            for t in all_trades:
                if t.get("status") == "CLOSED":
                    restored_balance += float(t.get("realized_pnl_usd") or 0.0)
                else:
                    restored_balance -= float(t.get("size_usd") or 0.0) + float(t.get("fee_usd") or 0.0)
            self.balance_usd = restored_balance
        except Exception:
            logger.exception("Could not reconstruct paper balance from ledger; keeping in-memory value")

        # A resting maker order cannot survive a restart (the in-memory
        # registry is rebuilt empty — the maker_orders rows are the audit
        # trail only). Mark any PENDING rows CANCELLED so the trail doesn't
        # show phantom open orders. Their cash was never in the ledger
        # (reserved only in memory), so the reconstructed balance above is
        # already correct.
        try:
            for row in await self.db.get_maker_orders(status="PENDING"):
                await self.db.resolve_maker_order(
                    row["id"], status="CANCELLED",
                    notes="process restart — resting order dropped with the broker connection",
                )
        except Exception:
            logger.exception("Could not clean stale PENDING maker orders on load")

        if open_trades:
            logger.info(
                "Restored %d open paper position(s) across %d market(s) from DB, balance $%.2f",
                len(open_trades), len(self._open_positions), self.balance_usd,
            )
        return len(open_trades)

    def has_open_position(self, market_id: str) -> bool:
        return bool(self._open_positions.get(market_id))

    async def get_total_exposure_usd(self) -> float:
        """Sum of size_usd across every currently open position — the input
        to the portfolio-level MAX_TOTAL_EXPOSURE_PCT cap, distinct from the
        per-trade MAX_POSITION_PCT cap. Pending sum-to-one maker orders count
        too: their cash is already reserved at post (see post_sum_to_one_maker),
        so sizing a second entry against that money would overshoot the cap."""
        open_trades = await self.db.get_open_trades(mode=self.mode)
        exposure = sum(t["size_usd"] for t in open_trades)
        exposure += sum(o.reserve_usd for o in self._maker_orders.values())
        return exposure

    def has_pending_maker(self, market_id: str) -> bool:
        """True if a resting sum-to-one maker order is outstanding on this
        market — the scan must not post a second one on the same market."""
        o = self._maker_orders.get(market_id)
        return o is not None and o.status == "PENDING"

    async def get_equity(self, known_markets: dict[str, Market]) -> float:
        """
        True mark-to-market equity: cash + current value of every open
        position, valued against the live book rather than frozen at entry
        price. Using cash alone (get_balance()) as a stand-in for account
        health — the previous behavior — understates equity while winning
        positions are open (risking a false daily-halt trigger) and hides
        real unrealized losses until they land all at once at settlement.

        known_markets: market_id -> Market, supplied by the caller (main.py
        keeps this current from its discovery + open-position-polling loops)
        so this doesn't need its own discovery round trip. A position whose
        market isn't present falls back to cost basis (no mark-to-market
        update) rather than failing outright.
        """
        equity = self.balance_usd
        open_trades = await self.db.get_open_trades(mode=self.mode)
        for t in open_trades:
            market = known_markets.get(t["market_id"])
            if market is None or not t["entry_price"]:
                equity += t["size_usd"]
                continue
            shares = t["size_usd"] / t["entry_price"]
            token_id = market.token_id_yes if t["side"] == "YES" else market.token_id_no
            try:
                book = await self.feed.get_order_book(market.market_id, token_id)
                mark_price = book.mid if book.mid is not None else t["entry_price"]
            except Exception:
                mark_price = t["entry_price"]
            equity += shares * mark_price
        return equity

    def _walk_book_for_fill(self, book: OrderBook, size_usd: float) -> tuple[float, float]:
        """
        Simulate a FOK/IOC-style BUY market order by walking the ask side.
        Returns (avg_fill_price, shares_filled). Raises if there isn't enough
        depth to fill the requested size_usd.
        """
        remaining_usd = size_usd
        shares = 0.0
        cost = 0.0

        for level in book.asks:
            level_usd = level.price * level.size
            take_usd = min(remaining_usd, level_usd)
            take_shares = take_usd / level.price
            shares += take_shares
            cost += take_usd
            remaining_usd -= take_usd
            if remaining_usd <= 1e-9:
                break

        if remaining_usd > 1e-9:
            raise ValueError(
                f"Insufficient order-book depth to fill ${size_usd:.2f}; "
                f"only ${cost:.2f} fillable at current book"
            )

        avg_price = cost / shares if shares > 0 else 0.0
        return avg_price, shares

    def _walk_book_for_sale(self, book: OrderBook, shares_to_sell: float) -> tuple[float, float]:
        """Mirror of _walk_book_for_fill for a SELL (used by close_position_early):
        walks the bid side. Returns (avg_price, shares_filled) — filled may be
        less than requested if the book can't absorb the full size."""
        remaining_shares = shares_to_sell
        proceeds = 0.0
        filled_shares = 0.0

        for level in book.bids:
            take_shares = min(remaining_shares, level.size)
            proceeds += take_shares * level.price
            filled_shares += take_shares
            remaining_shares -= take_shares
            if remaining_shares <= 1e-9:
                break

        avg_price = proceeds / filled_shares if filled_shares > 0 else 0.0
        return avg_price, filled_shares

    def _walk_book_for_shares(self, book: OrderBook, max_shares: float) -> tuple[float, float]:
        """
        Simulate a BUY market order for up to `max_shares` SHARES by walking
        the ask side (equal-share sibling of _walk_book_for_fill, added
        2026-08-12). Returns (avg_price, shares_filled); raises ValueError if
        the book can't absorb the full `max_shares`.
        """
        remaining = max_shares
        shares = 0.0
        cost = 0.0
        for level in book.asks:
            take = min(remaining, level.size)
            shares += take
            cost += take * level.price
            remaining -= take
            if remaining <= 1e-9:
                break
        if remaining > 1e-9:
            raise ValueError(
                f"Insufficient order-book depth to fill {max_shares:.2f} shares; "
                f"only {shares:.2f} available"
            )
        avg_price = cost / shares if shares > 0 else 0.0
        return avg_price, shares

    def _max_shares_within_budget(self, book: OrderBook, budget_usd: float) -> float:
        """Maximum shares buyable on the ask side within `budget_usd` dollars
        (walking from the best ask). Used to size an equal-share sum-to-one
        pair: the binding side's capacity caps the whole pair."""
        shares = 0.0
        cost = 0.0
        for level in book.asks:
            if cost >= budget_usd or level.price <= 0:
                break
            take_usd = budget_usd - cost
            take_shares = min(level.size, take_usd / level.price)
            shares += take_shares
            cost += take_shares * level.price
        return shares

    async def place_order(
        self,
        market: Market,
        side: str,
        size_usd: float,
        strategy: str = "latency_arb",
        combo_group_id: Optional[str] = None,
        book_source: Optional[object] = None,
        max_entry_price: Optional[float] = None,
    ) -> Fill:
        """
        side: "YES" or "NO". Walks the real live order book for the
        corresponding token to compute a realistic average fill price.

        If simulated_fill_latency_s > 0, this deliberately waits before
        consulting the book for the actual fill — simulating the real delay
        between "we decided to trade" and "the order lands" — so slippage
        reflects genuine book movement during that window (read from the
        continuously-updating WS cache) rather than an idealized instant fill
        against a frozen snapshot.
        """
        if size_usd < self.min_order_size_usd:
            raise OrderTooSmallError(
                f"${size_usd:.2f} is below the minimum order size of ${self.min_order_size_usd:.2f}"
            )
        if size_usd > self.balance_usd:
            raise InsufficientBalanceError(
                f"Requested ${size_usd:.2f} exceeds paper balance ${self.balance_usd:.2f}"
            )

        token_id = market.token_id_yes if side == "YES" else market.token_id_no

        async def _current_book() -> OrderBook:
            # book_source lets the event-driven fast path fill against the
            # continuously-updated WS book cache instead of a REST round trip
            # (which would add ~100-300ms back into the arbitrage window). When
            # provided, fall back to the normal REST fetch only if the cache
            # has no book for this token yet.
            if book_source is not None:
                cached = book_source(token_id)
                if cached is not None:
                    return cached
            return await self.feed.get_order_book(market.market_id, token_id)

        book_at_decision = await _current_book()
        mid_before = book_at_decision.mid
        decision_ask = book_at_decision.best_ask

        if self.simulated_fill_latency_s > 0:
            await asyncio.sleep(self.simulated_fill_latency_s)
            book = await _current_book()
        else:
            book = book_at_decision
        fill_ask = book.best_ask

        avg_price, shares = self._walk_book_for_fill(book, size_usd)
        avg_price = _round_to_tick(avg_price, self.tick_size)
        # Entry-price cap on the ACTUAL fill, not just the decision-time
        # best ask. The signal engine rejects when best_ask > cap, but the
        # fill walks the book after simulated latency and pays the average —
        # on thin 5-min books the ask can move from under the cap to 0.99
        # between decision and fill (live 2026-08-11: two entries decided at
        # 0.20/0.49 filled at 0.99 and settled at 0). Opening at a price the
        # strategy never approved turns a capped-away bad trade into a
        # realized near-total loss.
        if max_entry_price is not None and avg_price > max_entry_price:
            raise EntryPriceExceededError(
                f"Refused fill at avg ${avg_price:.3f} > max entry "
                f"${max_entry_price:.2f} (decision ask was "
                f"{decision_ask}, book moved during fill latency)"
            )
        # Price-dependent taker fee. Polymarket's formula is per SHARE
        # (rate * p * (1 - p)); as a fraction of what we SPEND that is
        # rate * (1 - p) — ~3.5% of notional at p=0.50, less at higher
        # prices. The old flat fee_pct assumption understated mid-price fees.
        fee_usd = size_usd * fees.taker_fee_fraction_of_notional(avg_price, self._fee_rate_for(market))

        total_cost = size_usd + fee_usd
        if total_cost > self.balance_usd:
            raise InsufficientBalanceError(
                f"Order cost incl. fees ${total_cost:.2f} exceeds paper balance ${self.balance_usd:.2f}"
            )

        # Fill-realism measurement (verified 2026-08-07): the paper broker
        # computes slippage on every fill but previously threw it away — so
        # the most useful paper-mode metric (how much edge is lost between
        # decision and fill) was unanswerable. Persist slippage + the
        # decision-time and fill-time best asks so edge decay is measurable.
        slippage_pct = 0.0
        if mid_before:
            slippage_pct = (avg_price - mid_before) / mid_before

        return await self._record_trade(
            market, side, size_usd, avg_price, fee_usd,
            strategy=strategy, combo_group_id=combo_group_id,
            slippage_pct=slippage_pct,
            decision_best_ask=decision_ask, fill_best_ask=fill_ask,
            shares=shares,
        )

    async def _record_trade(
        self,
        market: Market,
        side: str,
        size_usd: float,
        avg_price: float,
        fee_usd: float,
        *,
        strategy: str,
        combo_group_id: Optional[str],
        slippage_pct: float,
        decision_best_ask: Optional[float],
        fill_best_ask: Optional[float],
        shares: float,
        deduct_balance: bool = True,
    ) -> Fill:
        """
        Record an opened position in the DB + in-memory tracker and return
        its Fill. Shared by place_order (marketable taker fills — balance is
        deducted here, total_cost = size + fee) and the sum-to-one maker flow
        (the maker leg fills at its resting price with ZERO fee; the pair's
        cash was already reserved at post, so deduct_balance=False prevents
        double-counting the reservation).
        """
        if deduct_balance:
            self.balance_usd -= size_usd + fee_usd
        trade_id = await self.db.open_trade(
            signal_id=None,
            market_id=market.market_id,
            asset=market.asset,
            side=side,
            mode=self.mode,
            entry_price=avg_price,
            size_usd=size_usd,
            fee_usd=fee_usd,
            strategy=strategy,
            combo_group_id=combo_group_id,
            slippage_pct=slippage_pct,
            decision_best_ask=decision_best_ask,
            fill_best_ask=fill_best_ask,
        )
        self._open_positions.setdefault(market.market_id, []).append(trade_id)
        await self.db.record_equity(mode=self.mode, balance_usd=self.balance_usd)

        logger.info(
            "[PAPER] Filled %s %s $%.2f @ avg %.4f (slippage %.2f%%, fee $%.2f, strategy=%s)",
            side, market.market_id, size_usd, avg_price, slippage_pct * 100, fee_usd, strategy,
        )
        return Fill(
            trade_id=trade_id, market_id=market.market_id, side=side,
            avg_price=avg_price, size_usd=size_usd, shares=shares,
            fee_usd=fee_usd, slippage_pct=slippage_pct,
        )

    async def place_sum_to_one_order(
        self, opportunity: SumToOneOpportunity, total_size_usd: float,
        book_source: Optional[object] = None,
    ) -> tuple[Fill, Fill]:
        """
        Buys the SAME NUMBER OF SHARES of YES and NO (equal-SHARE sizing,
        fixed 2026-08-12) so the payout is identical regardless of outcome —
        the property that makes sum-to-one actual risk-free arbitrage. The
        previous equal-DOLLAR sizing ($50 YES @ 0.40 = 125 shares vs $50 NO
        @ 0.50 = 100 shares) paid $125 if YES won and $100 if NO won — a
        residual directional bet dressed up as risk-free. Now the share count
        is the maximum BOTH books' depth supports within the caller's dollar
        budget, and both legs open at exactly that count.
        combo_group_id links the pair so settlement and reporting treat them
        as one position.
        """
        combo_group_id = str(uuid.uuid4())
        half_budget = total_size_usd / 2
        rate = self._fee_rate_for(opportunity.market)

        async def _book(token_id: str) -> OrderBook:
            if book_source is not None:
                cached = book_source(token_id)
                if cached is not None:
                    return cached
            return await self.feed.get_order_book(opportunity.market.market_id, token_id)

        yes_book = await _book(opportunity.market.token_id_yes)
        no_book = await _book(opportunity.market.token_id_no)

        # (1) Equal-share capacity: the max shares EACH book absorbs within
        # its half-budget; the binding (thinner) side caps the whole pair.
        shares_target = min(
            self._max_shares_within_budget(yes_book, half_budget),
            self._max_shares_within_budget(no_book, half_budget),
        )
        if shares_target <= 0:
            raise SumToOneEdgeLostError(
                0.0, 0.0,
                action="refused before placing — no equal-share size both books support",
            )

        # (2) Quote the REAL equal-share fills before opening anything: the
        # opportunity was detected against best asks, but each leg fills by
        # WALKING the ask side — on a thin book the walked combined cost can
        # exceed $1 even when the best asks sum below it (live 2026-08-09:
        # fills at 0.86+0.15=1.01). Refuse a combo whose real equal-share
        # walk no longer locks a profit.
        try:
            yes_avg, _ = self._walk_book_for_shares(yes_book, shares_target)
            no_avg, _ = self._walk_book_for_shares(no_book, shares_target)
        except ValueError:
            raise SumToOneEdgeLostError(
                0.0, 0.0,
                action="refused before placing — insufficient book depth to fill both legs equally",
            )
        per_share_cost = (
            yes_avg + no_avg
            + fees.taker_fee_per_share(yes_avg, rate)
            + fees.taker_fee_per_share(no_avg, rate)
        )
        locked_edge_per_share = 1.0 - per_share_cost
        if locked_edge_per_share <= 0:
            logger.warning(
                "Sum-to-one %s refused before placing: equal-share fills %.3f+%.3f=%.3f "
                "(fees %.3f) — the real walk does not lock a profit",
                opportunity.market.market_id, yes_avg, no_avg,
                yes_avg + no_avg, per_share_cost - yes_avg - no_avg,
            )
            raise SumToOneEdgeLostError(
                per_share_cost, locked_edge_per_share,
                action="refused before placing — the real equal-share walk exceeds $1",
            )

        # (3) Affordability at the WALKED prices: shrink to the whole shares
        # the budget supports, or refuse when even one pair can't be
        # afforded. Validating the TOTAL up front (not per-leg) is what
        # prevents a half-open hedge (reviewed 2026-08-07).
        affordable = self.balance_usd / per_share_cost
        if affordable < 1.0:
            raise InsufficientBalanceError(
                f"Balance ${self.balance_usd:.2f} cannot afford one equal-share pair "
                f"(per-share cost ${per_share_cost:.3f})"
            )
        if affordable < shares_target:
            shares_target = int(affordable)
            yes_avg, _ = self._walk_book_for_shares(yes_book, shares_target)
            no_avg, _ = self._walk_book_for_shares(no_book, shares_target)
            per_share_cost = (
                yes_avg + no_avg
                + fees.taker_fee_per_share(yes_avg, rate)
                + fees.taker_fee_per_share(no_avg, rate)
            )
            locked_edge_per_share = 1.0 - per_share_cost
            if locked_edge_per_share <= 0:
                raise SumToOneEdgeLostError(
                    per_share_cost, locked_edge_per_share,
                    action="refused before placing — shrunk equal-share walk exceeds $1",
                )
        total_cost = shares_target * per_share_cost
        if total_cost > self.balance_usd:
            raise InsufficientBalanceError(
                f"Sum-to-one combo cost incl. fees ${total_cost:.2f} exceeds "
                f"paper balance ${self.balance_usd:.2f}"
            )

        # (4) Submit BOTH legs concurrently at exactly `shares_target`
        # shares. asyncio.gather overlaps the two fill-latency waits so both
        # legs fill against a ~simultaneous book (the sequential path let the
        # book move twice between the halves of the hedge — the −$45.94
        # loss). If one leg fails, reverse the leg that opened so the hedge
        # is never left half-open.
        results = await asyncio.gather(
            self._place_equal_share_leg(
                opportunity.market, "YES", shares_target, yes_avg,
                combo_group_id, book_source,
            ),
            self._place_equal_share_leg(
                opportunity.market, "NO", shares_target, no_avg,
                combo_group_id, book_source,
            ),
            return_exceptions=True,
        )
        failed = [r for r in results if isinstance(r, Exception)]
        if failed:
            for fill in results:
                if isinstance(fill, Exception):
                    continue
                try:
                    pnl = await self.close_position_early(
                        opportunity.market, fill.trade_id, reason="SUM_TO_ONE_LEG_FAILED",
                    )
                except Exception:
                    logger.exception("Failed to reverse sum-to-one leg after sibling failure")
                else:
                    if pnl is None:
                        logger.warning(
                            "Sum-to-one leg %d could NOT be reversed after sibling failure "
                            "(no bid depth) — half-open hedge remains on %s",
                            fill.trade_id, opportunity.market.market_id,
                        )
            raise failed[0]
        yes_fill, no_fill = results  # type: ignore[assignment]

        # (5) Re-validate the locked edge from the ACTUAL fills. Each leg
        # filled after the simulated fill latency against a book that kept
        # moving, so the combined cost can drift above $1 before both legs
        # land (verified live: 0.31+0.73=1.04, 0.17+0.89=1.06). With EQUAL
        # shares, holding to settlement is now exactly risk-free at a
        # positive edge — _resolve_edge_loss only reverses when it loses
        # strictly less than the (now exact) worst-case hold.
        combined_cost = yes_fill.avg_price + no_fill.avg_price
        fee_cost = (
            fees.taker_fee_pct(yes_fill.avg_price, rate)
            + fees.taker_fee_pct(no_fill.avg_price, rate)
        )
        locked_edge_pct = (1.0 - combined_cost) - fee_cost
        if locked_edge_pct <= 0:
            action = await self._resolve_edge_loss(
                opportunity.market, yes_fill, no_fill, combo_group_id,
            )
            raise SumToOneEdgeLostError(combined_cost, locked_edge_pct, action=action)

        return yes_fill, no_fill

    async def _place_equal_share_leg(
        self, market: Market, side: str, shares: float,
        quoted_avg: float, combo_group_id: str, book_source: Optional[object],
    ) -> Fill:
        """
        Open ONE leg of an equal-share sum-to-one pair at EXACTLY `shares`
        shares. Waits the simulated fill latency, re-walks the live book at
        `shares` (the book may have moved), then records the trade at the
        walked price with its price-dependent fee. Raises on failure so the
        caller can reverse the sibling leg — a half-open hedge must never
        persist silently.
        """
        token_id = market.token_id_yes if side == "YES" else market.token_id_no

        async def _book() -> OrderBook:
            if book_source is not None:
                cached = book_source(token_id)
                if cached is not None:
                    return cached
            return await self.feed.get_order_book(market.market_id, token_id)

        if self.simulated_fill_latency_s > 0:
            await asyncio.sleep(self.simulated_fill_latency_s)
        book = await _book()
        avg_price, filled = self._walk_book_for_shares(book, shares)
        if filled < shares - 1e-9:
            raise ValueError(
                f"Equal-share leg {side}: only {filled:.2f} of {shares:.2f} shares "
                "fillable after fill latency"
            )
        avg_price = _round_to_tick(avg_price, self.tick_size)
        rate = self._fee_rate_for(market)
        size_usd = shares * avg_price
        fee_usd = shares * fees.taker_fee_per_share(avg_price, rate)
        if size_usd + fee_usd > self.balance_usd:
            raise InsufficientBalanceError(
                f"Equal-share leg {side} cost ${size_usd + fee_usd:.2f} exceeds "
                f"paper balance ${self.balance_usd:.2f}"
            )
        return await self._record_trade(
            market, side, size_usd, avg_price, fee_usd,
            strategy="sum_to_one", combo_group_id=combo_group_id,
            slippage_pct=0.0, decision_best_ask=quoted_avg, fill_best_ask=avg_price,
            shares=filled,
        )

    # --- Sum-to-one MAKER execution (added 2026-08-12) ---------------------
    # Takers pay rate * p * (1 - p) per share on BOTH legs; MAKERS pay zero
    # and earn a rebate (20% on crypto). Post the CHEAPER leg as a resting
    # buy at the bid — its fee fraction of notional (rate * (1 - p)) is the
    # LARGER of the two, so zeroing its fee saves the most, and bid < ask
    # makes the entry price strictly better. The instant the maker leg
    # fills, take the other leg at market. The exposure gap between the legs
    # is ONE book read, not an open-ended cancel-and-reverse wait (Claude
    # review 2026-08-12: a both-legs-maker plan reintroduces exactly the
    # naked-position window the momentum_fallback disaster closed off —
    # never carry unhedged inventory longer than one round trip).

    async def post_sum_to_one_maker(
        self, market: Market, total_size_usd: float,
        book_source: Optional[object] = None,
    ) -> Optional[MakerOrder]:
        """
        Post ONE leg of a sub-$1 YES+NO pair as a resting BUY at the bid.
        Returns the MakerOrder, or None when this pair can't be posted as a
        maker right now (no bid on the cheap side, the maker+taker combo no
        longer locks a profit, or the taker leg's book can't absorb our
        size) — the caller may fall back to the taker flow.

        The maker leg is the CHEAPER side: its ask is lower, its fee fraction
        of notional is the larger, so making it the maker saves the most fee.
        The taker leg (expensive side) is taken at market the instant the
        maker fills.

        Depth-aware sizing: the size is capped by what the TAKER leg's ask
        side can actually absorb, because a maker fill MUST be paired — an
        unpaired fill is a naked directional position. Cash for the full pair
        (+ estimated taker fee) is RESERVED at post and refunded on cancel;
        the reservation also counts toward MAX_TOTAL_EXPOSURE_PCT via
        get_total_exposure_usd().
        """
        if self.has_pending_maker(market.market_id):
            return None

        async def _book(token_id: str) -> OrderBook:
            if book_source is not None:
                cached = book_source(token_id)
                if cached is not None:
                    return cached
            return await self.feed.get_order_book(market.market_id, token_id)

        yes_book = await _book(market.token_id_yes)
        no_book = await _book(market.token_id_no)
        yes_ask, no_ask = yes_book.best_ask, no_book.best_ask
        if yes_ask is None or no_ask is None:
            return None

        # Cheaper side -> maker leg; expensive side -> taken on fill.
        if yes_ask <= no_ask:
            maker_side, taker_side = "YES", "NO"
            maker_book, taker_book = yes_book, no_book
        else:
            maker_side, taker_side = "NO", "YES"
            maker_book, taker_book = no_book, yes_book

        maker_price = maker_book.best_bid
        taker_ask = taker_book.best_ask
        if maker_price is None or taker_ask is None or maker_price <= 0:
            return None

        rate = self._fee_rate_for(market)
        half = total_size_usd / 2

        # Lock check against the REAL post prices: the maker fills at the
        # bid, the taker at the ask. The maker leg pays zero fee; only the
        # taker leg's price-dependent fee applies. Must clear the same
        # minimum edge as the taker flow.
        locked_edge_pct = (1.0 - maker_price - taker_ask) - fees.taker_fee_pct(taker_ask, rate)
        if locked_edge_pct <= settings.SUM_TO_ONE_MIN_EDGE_PCT:
            return None

        # Equal-share sizing (2026-08-12): the pair must be the SAME share
        # count on both legs so the payout is identical regardless of
        # outcome. `target_shares` is bounded by the caller's dollar budget
        # at the current maker+taker prices, AND by the taker book's depth —
        # a maker fill must be pairable, or it becomes a naked position
        # (Claude review 2026-08-12).
        per_share_cost_est = (
            maker_price + taker_ask + fees.taker_fee_per_share(taker_ask, rate)
        )
        target_shares = total_size_usd / per_share_cost_est
        try:
            self._walk_book_for_shares(taker_book, target_shares)
        except ValueError:
            return None
        if target_shares <= 0:
            return None

        # Reserve cash for the full pair (both legs + estimated taker fee).
        # Refunded on cancel; adjusted to the actual fill on pair/reverse.
        est_taker_fee = target_shares * fees.taker_fee_per_share(taker_ask, rate)
        reserve = target_shares * (maker_price + taker_ask) + est_taker_fee
        if reserve > self.balance_usd:
            return None
        self.balance_usd -= reserve

        maker_token_id = market.token_id_yes if maker_side == "YES" else market.token_id_no
        db_row_id = await self.db.log_maker_order(
            market_id=market.market_id, side=maker_side, token_id=maker_token_id,
            price=maker_price, size_usd=half,
            combo_group_id=None,
            notes=(
                f"maker={maker_side}@{maker_price:.3f} taker={taker_side}@ask "
                f"locked_edge={locked_edge_pct:.2%} target_shares={target_shares:.1f}"
            ),
        )
        order = MakerOrder(
            market=market, market_id=market.market_id,
            maker_side=maker_side, taker_side=taker_side,
            maker_price=maker_price, half_size_usd=half,
            target_shares=target_shares,
            reserve_usd=reserve, posted_at=time.time(), db_row_id=db_row_id,
        )
        self._maker_orders[market.market_id] = order
        logger.info(
            "[PAPER] Posted sum-to-one maker %s %s %.1f shares @ bid %.3f "
            "(taker leg %s at ask %.3f, locked edge %.2f%%, reserved $%.2f)",
            maker_side, market.market_id, target_shares, maker_price,
            taker_side, taker_ask, locked_edge_pct * 100, reserve,
        )
        return order

    async def check_sum_to_one_makers(self) -> list[str]:
        """
        Advance every resting maker order one cycle (called from the sum-to-
        one scan BEFORE scanning new opportunities):
          - timed out          -> cancel (refund reserve, resolve CANCELLED)
          - filled + lock holds -> immediately take the other leg at market,
                                   open BOTH legs as a combo (resolve FILLED)
          - filled + lock gone  -> reverse the maker leg at market (resolve
                                   REVERSED) — NEVER take a taker leg that
                                   breaks the lock
        Returns short action strings for the caller's alert/log.
        """
        actions: list[str] = []
        for market_id, order in list(self._maker_orders.items()):
            if order.status != "PENDING":
                self._maker_orders.pop(market_id, None)
                continue
            try:
                action = await self._advance_maker_order(order)
            except Exception:
                logger.exception(
                    "Sum-to-one maker check failed for %s — keeping order pending", market_id,
                )
                continue
            if action:
                actions.append(action)
                if order.status != "PENDING":
                    self._maker_orders.pop(market_id, None)
        return actions

    async def _advance_maker_order(self, order: MakerOrder) -> Optional[str]:
        """One cycle of a single PENDING maker order. See check_sum_to_one_makers."""
        market = order.market
        rate = self._fee_rate_for(market)

        # (1) Timeout -> cancel and refund.
        if time.time() - order.posted_at > settings.SUM_TO_ONE_MAKER_TIMEOUT_S:
            self.balance_usd += order.reserve_usd
            await self.db.resolve_maker_order(
                order.db_row_id, status="CANCELLED",
                notes="timeout — no fill within SUM_TO_ONE_MAKER_TIMEOUT_S",
            )
            order.status = "CANCELLED"
            logger.info(
                "[PAPER] Sum-to-one maker %s timed out after %.0fs — cancelled, refunded $%.2f",
                market.market_id, settings.SUM_TO_ONE_MAKER_TIMEOUT_S, order.reserve_usd,
            )
            return f"maker_cancelled_timeout {market.market_id}"

        # (2) Fill detection with a CONSERVATIVE queue model (fixed
        # 2026-08-12, ChatGPT review): the resting bid sits at the BACK of
        # the queue at its price — sellers crossing down to our level first
        # consume the queue AHEAD of us (approximated as our own order size)
        # before we fill. Ask depth at-or-below our price less than our own
        # size only proves the PRICE touched our level, not that we were
        # reached. A fill therefore requires crossing volume to EXCEED our
        # size; partial fills are handled honestly — only the filled portion
        # is paired.
        maker_token = market.token_id_yes if order.maker_side == "YES" else market.token_id_no
        maker_book = await self.feed.get_order_book(market.market_id, maker_token)
        crossing_shares = sum(
            lvl.size for lvl in maker_book.asks if lvl.price <= order.maker_price + 1e-9
        )
        if crossing_shares <= 0:
            return None  # still resting

        queue_ahead = order.target_shares  # conservative: at least our size is ahead
        available = crossing_shares - queue_ahead
        if available <= 1e-9:
            return None  # price touched our level but the queue ahead wasn't consumed
        fill_shares = min(order.target_shares, available)
        fill_usd = fill_shares * order.maker_price

        # (3) The maker filled — immediately take the other leg at the SAME
        # share count (equal-share pair), but only if the lock still holds.
        # Quote the taker walk BEFORE opening anything.
        taker_token = market.token_id_yes if order.taker_side == "YES" else market.token_id_no
        taker_book = await self.feed.get_order_book(market.market_id, taker_token)
        try:
            taker_avg, taker_shares = self._walk_book_for_shares(taker_book, fill_shares)
        except ValueError:
            return await self._reverse_maker(
                order, fill_shares, fill_usd,
                reason="taker leg book cannot absorb the fill",
            )
        if taker_shares < fill_shares - 1e-9:
            return await self._reverse_maker(
                order, fill_shares, fill_usd,
                reason="taker leg filled only partially — cannot pair equal shares",
            )
        taker_avg = _round_to_tick(taker_avg, self.tick_size)

        # The lock is the same computation as post, against the ACTUAL taker
        # walk: maker leg fee = 0, taker leg pays its price-dependent fee.
        locked_edge_pct = (1.0 - order.maker_price - taker_avg) - fees.taker_fee_pct(taker_avg, rate)
        if locked_edge_pct <= 0:
            # Lock died before the taker leg was taken. Reverse the maker
            # leg NOW — never take a taker leg that turns a guaranteed
            # profit into a guaranteed loss. Entry was at the BID, so the
            # reversal loss is bounded by the spread (strictly smaller than
            # the taker flow's ask-walk reversal).
            return await self._reverse_maker(
                order, fill_shares, fill_usd,
                reason=(
                    f"lock broken at fill (maker {order.maker_price:.3f} + "
                    f"taker walk {taker_avg:.3f})"
                ),
            )

        # (4) Lock holds — open BOTH legs as a combo (maker fee = 0), then
        # refund the unused reservation (estimated vs actual taker fee). The
        # pair settles normally via settle_position(), like the taker flow's
        # combos.
        combo_group_id = str(uuid.uuid4())
        maker_fee = 0.0
        taker_fee = fill_shares * fees.taker_fee_per_share(taker_avg, rate)
        maker_size_usd = fill_shares * order.maker_price
        taker_size_usd = fill_shares * taker_avg
        await self._record_trade(
            market, order.maker_side, maker_size_usd, order.maker_price, maker_fee,
            strategy="sum_to_one", combo_group_id=combo_group_id,
            slippage_pct=0.0, decision_best_ask=order.maker_price,
            fill_best_ask=order.maker_price, shares=fill_shares,
            deduct_balance=False,
        )
        await self._record_trade(
            market, order.taker_side, taker_size_usd, taker_avg, taker_fee,
            strategy="sum_to_one", combo_group_id=combo_group_id,
            slippage_pct=0.0, decision_best_ask=taker_book.best_ask,
            fill_best_ask=taker_avg, shares=taker_shares,
            deduct_balance=False,
        )
        # Both legs now open at the SAME share count (equal-share pair); the
        # reservation covered the estimated cost, refund/adjust the delta.
        actual_cost = maker_size_usd + taker_size_usd + taker_fee
        self.balance_usd += order.reserve_usd - actual_cost

        await self.db.resolve_maker_order(
            order.db_row_id, status="FILLED",
            filled_price=order.maker_price, taker_leg_price=taker_avg,
            combined_cost=order.maker_price + taker_avg, taker_fee_usd=taker_fee,
            notes=f"paired as combo {combo_group_id}",
        )
        order.status = "FILLED"
        logger.info(
            "[PAPER] Sum-to-one maker %s filled @ %.3f -> took %s @ %.3f: pair cost %.3f "
            "(locked edge %.2f%%), combo %s",
            market.market_id, order.maker_price, order.taker_side, taker_avg,
            order.maker_price + taker_avg, locked_edge_pct * 100, combo_group_id,
        )
        return f"maker_paired {market.market_id} (edge {locked_edge_pct:.2%})"

    async def _reverse_maker(
        self, order: MakerOrder, fill_shares: float, fill_usd: float, reason: str,
    ) -> str:
        """
        The maker leg filled but the pair can't be completed safely. Sell
        the filled shares at market immediately — one round trip of exposure,
        never an open-ended naked position. The maker entered at the BID, so
        the reversal loss is bounded by the spread.

        Cash: the full pair was reserved at post; refund the reservation,
        then apply the maker leg's own P&L (paid fill_usd, received the bid-
        side walk net of exit fee).
        """
        market = order.market
        rate = self._fee_rate_for(market)
        maker_token = market.token_id_yes if order.maker_side == "YES" else market.token_id_no
        book = await self.feed.get_order_book(market.market_id, maker_token)
        price, filled = self._walk_book_for_sale(book, fill_shares)
        if filled <= 0 or filled < fill_shares - 1e-9:
            # Cannot exit the FULL position (no depth or partial only) — the
            # position rides to settlement as a directional hold. Recorded
            # loudly: this is the same class of residual risk the taker
            # flow's half-open-leg fallback has. The maker leg IS recorded
            # as a real (unpaired) trade so its settlement P&L is realized
            # honestly instead of vanishing; the maker_orders row is marked
            # HELD to keep the audit trail. Cash: the reservation minus the
            # fill cost is refunded (the fill itself paid fill_usd); the
            # trade is opened with deduct_balance=False since that cash is
            # already accounted for.
            logger.warning(
                "[PAPER] Sum-to-one maker %s fill could NOT be reversed (%s) — "
                "bid side absorbs only %.2f of %.2f shares; holding %s %.2f shares to settlement",
                market.market_id, reason, filled, fill_shares, order.maker_side, fill_shares,
            )
            self.balance_usd += order.reserve_usd - fill_usd
            await self._record_trade(
                market, order.maker_side, fill_usd, order.maker_price, 0.0,
                strategy="sum_to_one", combo_group_id=None,
                slippage_pct=0.0, decision_best_ask=order.maker_price,
                fill_best_ask=order.maker_price, shares=fill_shares,
                deduct_balance=False,
            )
            await self.db.resolve_maker_order(
                order.db_row_id, status="HELD", filled_price=order.maker_price,
                notes=f"{reason}; bid depth absorbs {filled:.2f}/{fill_shares:.2f} shares — held to settlement",
            )
            order.status = "HELD"
            return f"maker_held_to_settlement {market.market_id}"

        proceeds = filled * price
        exit_fee = proceeds * fees.taker_fee_fraction_of_notional(price, rate)
        net_proceeds = proceeds - exit_fee
        self.balance_usd += order.reserve_usd - fill_usd + net_proceeds
        await self.db.resolve_maker_order(
            order.db_row_id, status="REVERSED", filled_price=order.maker_price,
            notes=(
                f"{reason}; sold {filled:.2f} shares @ {price:.3f} "
                f"(net ${net_proceeds:.2f})"
            ),
        )
        order.status = "REVERSED"
        logger.info(
            "[PAPER] Sum-to-one maker %s reversed after fill (%s): sold %.2f shares "
            "@ %.3f, net $%.2f",
            market.market_id, reason, filled, price, net_proceeds,
        )
        return f"maker_reversed {market.market_id}"

    async def _quote_reversal_net(self, market: Market, fill: Fill) -> Optional[float]:
        """
        Net-of-exit-fee proceeds a reversal of `fill` would realize at the
        current bid side, or None if the bid side cannot absorb the whole
        leg (in which case a clean reversal is impossible).
        """
        token_id = market.token_id_yes if fill.side == "YES" else market.token_id_no
        try:
            book = await self.feed.get_order_book(market.market_id, token_id)
        except Exception:
            return None
        shares = fill.size_usd / fill.avg_price if fill.avg_price else 0.0
        price, filled = self._walk_book_for_sale(book, shares)
        if filled <= 0 or filled < shares - 1e-9:
            return None
        proceeds = filled * price
        exit_fee = proceeds * fees.taker_fee_fraction_of_notional(price, self._fee_rate_for(market))
        return proceeds - exit_fee

    async def _resolve_edge_loss(
        self, market: Market, yes_fill: Fill, no_fill: Fill, combo_group_id: str,
    ) -> str:
        """
        Post-fill edge-loss fallback: decide between reversing both legs now
        and holding the pair to settlement — whichever loses LESS.

        Holding's worst case is known: only the CHEAPER side's shares pay $1
        at settlement (equal-dollar sizing leaves unequal share counts), so
        the worst-case hold PnL is min(shares_yes, shares_no) − entry cost.
        Reversing's PnL is the current bid-side walk of both legs, net of
        exit fees. On thin books the reversal spread cost is usually the
        larger loss (live 2026-08-09: fills at 1.01, reversal sold at 0.865
        → −$46.72), so reversal is only taken when it is strictly better
        than the worst-case hold. If either leg can't be fully sold, holding
        is forced.

        Returns a short action description for the caller's exception.
        """
        yes_shares = yes_fill.size_usd / yes_fill.avg_price if yes_fill.avg_price else 0.0
        no_shares = no_fill.size_usd / no_fill.avg_price if no_fill.avg_price else 0.0
        entry_cost = yes_fill.size_usd + no_fill.size_usd + yes_fill.fee_usd + no_fill.fee_usd
        hold_worst_pnl = min(yes_shares, no_shares) - entry_cost

        reversal_pnl: Optional[float] = None
        net_proceeds = 0.0
        for fill in (yes_fill, no_fill):
            leg_net = await self._quote_reversal_net(market, fill)
            if leg_net is None:
                reversal_pnl = None
                break
            net_proceeds += leg_net
        else:
            # Convert gross proceeds to net PnL on the SAME basis as
            # hold_worst_pnl (net of the entry cost of both legs).
            reversal_pnl = net_proceeds - entry_cost

        if reversal_pnl is not None and reversal_pnl > hold_worst_pnl:
            logger.warning(
                "Sum-to-one edge lost at fill for combo %s: reversal PnL $%.2f beats "
                "worst-case hold $%.2f — reversing both legs",
                combo_group_id, reversal_pnl, hold_worst_pnl,
            )
            for fill in (yes_fill, no_fill):
                try:
                    pnl = await self.close_position_early(
                        market, fill.trade_id, reason="SUM_TO_ONE_EDGE_LOST",
                    )
                except Exception:
                    logger.exception("Failed to reverse sum-to-one leg %d after edge loss", fill.trade_id)
                else:
                    if pnl is None:
                        logger.warning(
                            "Sum-to-one leg %d could NOT be reversed after edge loss "
                            "(no full bid depth) — holding to settlement",
                            fill.trade_id,
                        )
            return "both legs reversed"

        logger.warning(
            "Sum-to-one edge lost at fill for combo %s: holding to settlement "
            "(worst-case hold PnL $%.2f vs reversal PnL %s) — the pair resolves normally",
            combo_group_id, hold_worst_pnl,
            f"${reversal_pnl:.2f}" if reversal_pnl is not None else "n/a (no full bid depth)",
        )
        return "both legs held to settlement"

    async def cancel_order(self, order_id: str) -> bool:
        """
        Interface-parity stub with LiveBroker.cancel_order — same name and
        parameters. Paper mode has no resting orders to cancel: every
        place_order() fills immediately by walking the live book (FOK-style),
        so there is nothing outstanding to cancel and this always reports
        success.
        """
        logger.debug("[PAPER] cancel_order(%s) — no resting orders in paper mode", order_id)
        return True

    async def cancel_all_orders(self, market_id: str | None = None) -> int:
        """
        Interface-parity stub with LiveBroker.cancel_all_orders — same name
        and parameters. Paper mode fills orders immediately, so there are no
        open resting orders to cancel; returns the count cancelled (always 0).
        """
        logger.debug(
            "[PAPER] cancel_all_orders(market_id=%s) — no resting orders in paper mode", market_id,
        )
        return 0

    def quote_exit(self, book: OrderBook, shares: float) -> tuple[float, float]:
        """
        Quote what an early-exit SELL would actually get: the walked bid-side
        average (price, shares_filled). THE DECISION AND THE FILL SHARE THIS
        EXACT COMPUTATION so an exit can never fire on a price the fill
        cannot reach. (The old decision used book.mid — halfway between bid
        and ask — while the fill walked the bid side; on the wide books of
        5-min crypto markets mid can show a +REPRICE gain the bid side
        cannot deliver, producing exits that "fire" and fill at the entry
        price: a guaranteed loss after fees.)
        """
        return self._walk_book_for_sale(book, shares)

    async def close_position_early(self, market: Market, trade_id: int, reason: str) -> Optional[float]:
        """
        Exits a specific open position before settlement, selling the held
        shares against the current live book. Without this, positions could
        only ever be held to expiry — no take-profit, no cutting a position
        whose edge has reversed.

        FOK semantics: the exit is REFUSED unless the bid side can absorb the
        ENTIRE position at the walked price. The old code closed the trade
        even on a partial fill — booking a loss equal to the unfilled
        remainder and deleting the position's unrealized value.
        """
        open_trades = await self.db.get_open_trades(mode=self.mode)
        trade = next((t for t in open_trades if t["id"] == trade_id), None)
        if trade is None:
            return None

        token_id = market.token_id_yes if trade["side"] == "YES" else market.token_id_no
        book = await self.feed.get_order_book(market.market_id, token_id)
        shares = trade["size_usd"] / trade["entry_price"] if trade["entry_price"] else 0.0

        exit_price, filled_shares = self._walk_book_for_sale(book, shares)
        if filled_shares <= 0:
            logger.warning("Could not exit trade %d for %s — no bid depth", trade_id, market.market_id)
            return None
        if filled_shares < shares - 1e-9:
            logger.warning(
                "Refusing exit of trade %d for %s: bid side absorbs only %.1f of %.1f shares "
                "(thin book) — holding position rather than closing at a phantom price",
                trade_id, market.market_id, filled_shares, shares,
            )
            return None

        proceeds = filled_shares * exit_price
        # Exit is a taker sell — the same price-dependent fee applies (fraction
        # of the proceeds, rate * (1 - p) at the exit price), category-aware.
        exit_fee = proceeds * fees.taker_fee_fraction_of_notional(exit_price, self._fee_rate_for(market))
        net_proceeds = proceeds - exit_fee
        realized_pnl = net_proceeds - trade["size_usd"] - trade["fee_usd"]

        self.balance_usd += net_proceeds
        await self.db.close_trade(
            trade_id, exit_price=exit_price, exit_reason=reason, realized_pnl_usd=realized_pnl,
        )
        await self.db.record_equity(mode=self.mode, balance_usd=self.balance_usd)

        positions = self._open_positions.get(market.market_id, [])
        if trade_id in positions:
            positions.remove(trade_id)
            if not positions:
                del self._open_positions[market.market_id]

        logger.info(
            "[PAPER] Early exit %s trade %d (%s): PnL $%.2f, balance now $%.2f",
            market.market_id, trade_id, reason, realized_pnl, self.balance_usd,
        )
        return realized_pnl

    async def settle_position(self, market: Market) -> Optional[float]:
        """
        Settle ALL open trades for this market at contract expiry (usually
        one; two for a sum-to-one pair), using the ACTUAL resolved outcome
        pulled from Gamma. Returns total realized PnL across whatever was
        settled, or None if there was nothing open or the market isn't
        resolved yet.

        Caller is responsible for supplying a Market whose resolution status
        has actually been checked directly (see PolymarketFeed.get_market_by_id)
        — NOT one pulled from discover_active_markets(), which filters out
        closed markets and would make this method effectively unreachable for
        anything that has actually resolved.
        """
        trade_ids = self._open_positions.get(market.market_id)
        if not trade_ids:
            return None

        outcome = await self.feed.get_market_outcome(market.market_id)
        if outcome is None:
            return None  # not resolved yet, caller should retry later

        open_trades = await self.db.get_open_trades(mode=self.mode)
        trades_to_settle = [t for t in open_trades if t["id"] in trade_ids]
        if not trades_to_settle:
            # In-memory tracker and DB disagree (shouldn't normally happen) —
            # clear the stale entry rather than retrying this forever.
            self._open_positions.pop(market.market_id, None)
            return None

        total_pnl = 0.0
        for trade in trades_to_settle:
            won = (trade["side"] == "YES" and outcome == "YES") or (trade["side"] == "NO" and outcome == "NO")
            shares = trade["size_usd"] / trade["entry_price"] if trade["entry_price"] else 0.0
            exit_price = 1.0 if won else 0.0
            payout = shares * exit_price
            realized_pnl = payout - trade["size_usd"] - trade["fee_usd"]

            self.balance_usd += payout
            await self.db.close_trade(
                trade["id"], exit_price=exit_price, exit_reason="SETTLED", realized_pnl_usd=realized_pnl,
            )
            total_pnl += realized_pnl
            logger.info(
                "[PAPER] Settled %s trade %d: %s, PnL $%.2f",
                market.market_id, trade["id"], "WIN" if won else "LOSS", realized_pnl,
            )

        await self.db.record_equity(mode=self.mode, balance_usd=self.balance_usd)
        del self._open_positions[market.market_id]

        logger.info(
            "[PAPER] Market %s fully settled, total PnL $%.2f, balance now $%.2f",
            market.market_id, total_pnl, self.balance_usd,
        )
        return total_pnl
