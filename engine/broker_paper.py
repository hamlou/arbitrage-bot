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
from engine.sum_to_one import SumToOneOpportunity
from storage.db import Database

logger = logging.getLogger(__name__)

DEFAULT_FEE_PCT = 0.02  # Polymarket's taker fee schedule changes over time — keep this config-driven.


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

        if self.simulated_fill_latency_s > 0:
            await asyncio.sleep(self.simulated_fill_latency_s)
            book = await _current_book()
        else:
            book = book_at_decision

        avg_price, shares = self._walk_book_for_fill(book, size_usd)
        avg_price = _round_to_tick(avg_price, self.tick_size)
        fee_usd = size_usd * self.fee_pct

        total_cost = size_usd + fee_usd
        if total_cost > self.balance_usd:
            raise InsufficientBalanceError(
                f"Order cost incl. fees ${total_cost:.2f} exceeds paper balance ${self.balance_usd:.2f}"
            )

        self.balance_usd -= total_cost

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
        yes_fill = await self.place_order(
            opportunity.market, "YES", half, strategy="sum_to_one", combo_group_id=combo_group_id,
            book_source=book_source,
        )
        no_fill = await self.place_order(
            opportunity.market, "NO", half, strategy="sum_to_one", combo_group_id=combo_group_id,
            book_source=book_source,
        )
        return yes_fill, no_fill

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

    async def close_position_early(self, market: Market, trade_id: int, reason: str) -> Optional[float]:
        """
        Exits a specific open position before settlement, selling the held
        shares against the current live book. Without this, positions could
        only ever be held to expiry — no take-profit, no cutting a position
        whose edge has reversed.
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

        proceeds = filled_shares * exit_price
        exit_fee = proceeds * self.fee_pct
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
