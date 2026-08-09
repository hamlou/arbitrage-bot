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


class InsufficientBalanceError(Exception):
    pass


class OrderTooSmallError(Exception):
    pass


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
        per-trade MAX_POSITION_PCT cap."""
        open_trades = await self.db.get_open_trades(mode=self.mode)
        return sum(t["size_usd"] for t in open_trades)

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

    async def place_order(
        self,
        market: Market,
        side: str,
        size_usd: float,
        strategy: str = "latency_arb",
        combo_group_id: Optional[str] = None,
        book_source: Optional[object] = None,
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
        # Price-dependent taker fee. Polymarket's formula is per SHARE
        # (rate * p * (1 - p)); as a fraction of what we SPEND that is
        # rate * (1 - p) — ~3.5% of notional at p=0.50, less at higher
        # prices. The old flat fee_pct assumption understated mid-price fees.
        fee_usd = size_usd * fees.taker_fee_fraction_of_notional(avg_price, self.fee_pct)

        total_cost = size_usd + fee_usd
        if total_cost > self.balance_usd:
            raise InsufficientBalanceError(
                f"Order cost incl. fees ${total_cost:.2f} exceeds paper balance ${self.balance_usd:.2f}"
            )

        self.balance_usd -= total_cost

        # Fill-realism measurement (verified 2026-08-07): the paper broker
        # computes slippage on every fill but previously threw it away — so
        # the most useful paper-mode metric (how much edge is lost between
        # decision and fill) was unanswerable. Persist slippage + the
        # decision-time and fill-time best asks so edge decay is measurable.
        slippage_pct = 0.0
        if mid_before:
            slippage_pct = (avg_price - mid_before) / mid_before

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
            decision_best_ask=decision_ask,
            fill_best_ask=fill_ask,
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
        Buys both YES and NO to lock in a risk-free profit regardless of
        outcome. combo_group_id links the two legs so settlement and
        reporting treat them as one position, not two unrelated directional
        bets.

        Sizing note: splits total_size_usd evenly by DOLLAR amount between
        the two legs, not by equal SHARE count. True equal-share sizing would
        require solving jointly against both books' depth; not worth the
        complexity for what are typically small, short-lived edges. Equal-
        dollar sizing means a small residual directional exposure can remain
        when yes_ask != no_ask — real, but usually minor relative to the
        locked-in edge. Documented here rather than silently assumed away.
        """
        combo_group_id = str(uuid.uuid4())
        half = total_size_usd / 2

        # (1) QUOTE the real fills BEFORE opening anything. The opportunity
        # was detected against best asks (find_sum_to_one_opportunity), but
        # each leg fills by WALKING the ask side — on a thin book the walked
        # combined cost can exceed $1 even when the best asks sum below it
        # (live 2026-08-09: fills at 0.86+0.15=1.01 — a guaranteed loss that
        # the reversal then amplified to −$46.72). Refuse to place at all
        # when the real walk no longer locks a profit: never open a
        # guaranteed-losing combo.
        quoted = await self._quote_sum_to_one_fills(opportunity.market, half, book_source)
        if quoted is None:
            raise SumToOneEdgeLostError(
                0.0, 0.0,
                action="refused before placing — insufficient book depth to fill both legs",
            )
        quoted_cost = quoted[0] + quoted[1]
        quoted_fee_cost = (
            fees.taker_fee_pct(quoted[0], self.fee_pct)
            + fees.taker_fee_pct(quoted[1], self.fee_pct)
        )
        quoted_edge = (1.0 - quoted_cost) - quoted_fee_cost
        if quoted_edge <= 0:
            logger.warning(
                "Sum-to-one %s refused before placing: walked fills %.3f+%.3f=%.3f "
                "(edge %.2f%%) — best asks looked under $1 but the real ask-walk does not",
                opportunity.market.market_id, quoted[0], quoted[1], quoted_cost, quoted_edge * 100,
            )
            raise SumToOneEdgeLostError(
                quoted_cost, quoted_edge,
                action="refused before placing — the real fill walk exceeds $1",
            )

        # (2) Pre-check the TOTAL cost (both legs + price-dependent fees on
        # the QUOTED fill prices) BEFORE opening either leg. Two reasons
        # (reviewed 2026-08-07): (1) with concurrent legs, two per-leg
        # balance checks could each pass while the combined cost exceeds the
        # balance — the total must be validated up front; (2) sequentially,
        # the second leg could raise InsufficientBalanceError after the
        # first already opened, leaving a HALF-OPEN hedge that the
        # edge-revalidation below never sees. Each leg is half the size, so
        # total fee = half*(fee(yes_fill) + fee(no_fill)) at the QUOTED
        # prices.
        fee_yes = half * fees.taker_fee_fraction_of_notional(quoted[0], self.fee_pct)
        fee_no = half * fees.taker_fee_fraction_of_notional(quoted[1], self.fee_pct)
        total_cost = total_size_usd + fee_yes + fee_no
        if total_cost > self.balance_usd:
            raise InsufficientBalanceError(
                f"Sum-to-one combo cost incl. fees ${total_cost:.2f} exceeds "
                f"paper balance ${self.balance_usd:.2f}"
            )

        # Submit BOTH legs concurrently. Sequential submission let the book
        # move for the full fill latency TWICE between the two halves of the
        # hedge (each leg pays simulated_fill_latency_s on its own) — fills
        # landed at 0.31+0.73=1.04 and 0.17+0.89=1.06, the −$45.94 loss.
        # asyncio.gather overlaps the two latency waits so both legs fill
        # against a ~simultaneous book. The per-leg balance checks each see
        # half*(1+fee) <= balance; since the total was pre-validated above,
        # both debits are safe.
        results = await asyncio.gather(
            self.place_order(
                opportunity.market, "YES", half, strategy="sum_to_one",
                combo_group_id=combo_group_id, book_source=book_source,
            ),
            self.place_order(
                opportunity.market, "NO", half, strategy="sum_to_one",
                combo_group_id=combo_group_id, book_source=book_source,
            ),
            return_exceptions=True,
        )
        failed = [r for r in results if isinstance(r, Exception)]
        if failed:
            # One leg failed (e.g. insufficient book depth). Reverse the leg
            # that DID open so the hedge is never left half-open, then
            # re-raise the original error. close_position_early returns None
            # (not an exception) when there is no bid depth to sell into —
            # that escape must be logged loudly, never silent, or a half-open
            # hedge would persist without a trace.
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

        # Re-validate the locked edge from the ACTUAL fills, not the
        # decision-time best asks. The opportunity was detected against best
        # asks, but each leg fills after the simulated fill latency against a
        # book that has kept moving, and the fill walks the ask side — so the
        # combined cost can drift above $1 before both legs land. Verified
        # 2026-08-07: fills landed at 0.31+0.73=1.04 and 0.17+0.89=1.06 (a
        # guaranteed loss), yet the decision-time edge check had passed.
        combined_cost = yes_fill.avg_price + no_fill.avg_price
        # Price-dependent fees per share: fee_rate * p * (1 - p) for each leg.
        fee_cost = (
            fees.taker_fee_pct(yes_fill.avg_price, self.fee_pct)
            + fees.taker_fee_pct(no_fill.avg_price, self.fee_pct)
        )
        locked_edge_pct = (1.0 - combined_cost) - fee_cost
        if locked_edge_pct <= 0:
            # The "arbitrage" is gone at fill — but reversing is NOT
            # automatically the right move: it sells both legs into the
            # current bids, whose spread cost can far exceed the cost of
            # holding the pair to settlement (live 2026-08-09: fills at 1.01,
            # reversal sold at 0.865 → −$46.72, while the worst-case hold
            # loss was similar and the best case profitable). _resolve_edge_loss
            # reverses only when it loses LESS than holding; otherwise it
            # holds to settlement, where the pair resolves normally.
            action = await self._resolve_edge_loss(
                opportunity.market, yes_fill, no_fill, combo_group_id,
            )
            raise SumToOneEdgeLostError(combined_cost, locked_edge_pct, action=action)

        return yes_fill, no_fill

    async def _quote_sum_to_one_fills(
        self, market: Market, half_size_usd: float, book_source: Optional[object],
    ) -> Optional[tuple[float, float]]:
        """
        Walk BOTH legs' ask sides and return the average fill price each leg
        would ACTUALLY get for half_size_usd, or None if either book cannot
        absorb its leg. This is the pre-place guard: the decision sees best
        asks (which can sum below $1) while the fill walks the whole ask
        side (which on a thin book sums above $1). Quote the real walk so a
        guaranteed-losing combo is never opened in the first place.
        """
        async def _book(token_id: str) -> OrderBook:
            if book_source is not None:
                cached = book_source(token_id)
                if cached is not None:
                    return cached
            return await self.feed.get_order_book(market.market_id, token_id)

        try:
            yes_avg, _ = self._walk_book_for_fill(await _book(market.token_id_yes), half_size_usd)
            no_avg, _ = self._walk_book_for_fill(await _book(market.token_id_no), half_size_usd)
        except ValueError:
            return None
        return yes_avg, no_avg

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
        exit_fee = proceeds * fees.taker_fee_fraction_of_notional(price, self.fee_pct)
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
        # of the proceeds, rate * (1 - p) at the exit price).
        exit_fee = proceeds * fees.taker_fee_fraction_of_notional(exit_price, self.fee_pct)
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
