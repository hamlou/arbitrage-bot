"""
Live broker implementation, built against py-clob-client-v2 (CLOB V2 — the only
version that works against production as of the 2026-04-28 cutover; see
https://docs.polymarket.com/v2-migration). Every method/enum used here was
verified against the installed package (`python3 -c "import py_clob_client_v2"`),
not guessed from memory — that distinction matters given how much changed in
this migration.

DO NOT WEAKEN THE GATE below. build_live_broker() is still the only way to get
an instance of this class, and it still requires all three
LIVE_TRADING_CONFIRMED_* flags plus PAPER_MODE=False, simultaneously.

On-chain redemption IS implemented: the verified redeemPositions(address,
bytes32, bytes32, uint256[]) call on the CtfCollateralAdapter lives in
engine/redeem.py (broadcast_redeem_tx, settings-free, shared with
scripts/manual_redeem.py) and is wrapped here as LiveBroker.redeem_position().
It is called from main.py's settlement loop for resolved live positions and
from scripts/manual_redeem.py for one-off manual redemption. settle_position()
remains intentionally unimplemented (see its docstring) — the loop calls the
verified redeem_position() primitive directly rather than a second, competing
settle path.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
from eth_account import Account
from py_clob_client_v2 import (
    AssetType,
    BalanceAllowanceParams,
    ClobClient,
    MarketOrderArgs,
    OrderMarketCancelParams,
    OrderPayload,
    OrderType,
    PartialCreateOrderOptions,
    Side,
)

from alerts.telegram import AlertLevel, TelegramAlerter
from config.settings import Settings
from data.polymarket_feed import Market
from engine.redeem import POLYGON_CHAIN_ID, broadcast_redeem_tx

logger = logging.getLogger(__name__)

DATA_API_BASE = "https://data-api.polymarket.com"  # positions/trades, no auth needed for reads

# On-chain redemption (constants + broadcast logic) lives in engine/redeem.py
# — the single, settings-free source of truth shared with
# scripts/manual_redeem.py: CTF_COLLATERAL_ADAPTER, REDEEM_ABI,
# PUSD_COLLATERAL_TOKEN, ZERO_BYTES32 and broadcast_redeem_tx. The verified
# call is redeemPositions(address, bytes32, bytes32, uint256[]) on the
# CtfCollateralAdapter, checked 2026-08 against docs.polymarket.com and the
# Polygonscan verified ABI.


class LiveTradingNotEnabledError(RuntimeError):
    """Raised when build_live_broker() is called without all gate conditions satisfied."""


@dataclass(frozen=True, slots=True)
class LiveFill:
    order_id: str
    market_id: str
    side: str            # "YES" / "NO"
    avg_price: float
    size_usd: float
    shares: float
    fee_usd: float
    raw_response: dict


class LiveBroker:
    """
    Same conceptual interface as PaperBroker (place_order, get_balance,
    settle_position), backed by real CLOB V2 order placement.
    """

    def __init__(self, private_key: str, alerter: TelegramAlerter, signature_type: int = 1):
        # private_key is held only in memory, never logged, never in __repr__.
        self._private_key = private_key
        self.alerter = alerter
        self._client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=POLYGON_CHAIN_ID,
            key=private_key,
            signature_type=signature_type,
        )
        creds = self._client.create_or_derive_api_key()
        self._client.set_api_creds(creds)
        self._data_client = httpx.AsyncClient(base_url=DATA_API_BASE, timeout=10.0)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "LiveBroker(...redacted...)"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.__repr__()

    @property
    def mode(self) -> str:
        return "LIVE"

    @property
    def wallet_address(self) -> str:
        """The wallet address derived from the private key — used for the
        read-only data-api positions lookup in the settlement loop."""
        return Account.from_key(self._private_key).address

    async def get_balance(self) -> float:
        """Returns pUSD collateral balance (CLOB V2's settlement asset, 1:1 with USDC)."""
        result = self._client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        # balance is returned in base units (micro-pUSD); divide by 1e6 for a dollar figure.
        return int(result["balance"]) / 1_000_000

    async def place_order(self, market: Market, side: str, size_usd: float) -> LiveFill:
        """
        Places a real FOK market order for size_usd worth of the given side.
        This is real money the moment it's called — build_live_broker()'s gate
        is what stands between this method and an accidental call.
        """
        token_id = market.token_id_yes if side == "YES" else market.token_id_no

        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=size_usd,   # USD-denominated for a BUY market order
            side=Side.BUY,
        )
        resp = self._client.create_and_post_market_order(
            order_args=order_args,
            options=PartialCreateOrderOptions(tick_size="0.01"),
            order_type=OrderType.FOK,
        )

        # Response field NAMES confirmed against py-clob-client-v2 (1.1.0)
        # source and the CLOB POST /order docs (verified 2026-08): the
        # response carries success, errorMsg, orderID, status, takingAmount,
        # makingAmount, transactionsHashes, tradeIDs. There is NO price, size,
        # makerAmount, or fee field — the pre-verification reads below were
        # wrong on every field except orderID. Interpretation that making =
        # shares / taking = USDC for a BUY is the documented reading, but has
        # NOT yet been confirmed against a live order — treat avg_price as
        # provisional until scripts/live_smoke_test.py runs against a real
        # fill.
        order_id = str(resp.get("orderID", "") or "")
        shares = float(resp.get("makingAmount", 0) or 0)
        taking_amount = float(resp.get("takingAmount", 0) or 0)
        avg_price = (taking_amount / shares) if shares > 0 else 0.0
        # Fee is not part of the order response; deriving the fill fee requires
        # a trade-level lookup (data-api /trades) that is not implemented yet —
        # left at 0.0 pending that investigation rather than guessed.
        fee_usd = 0.0

        logger.info(
            "[LIVE] Order submitted: %s %s $%.2f -> resp=%s",
            side, market.market_id, size_usd, resp,
        )

        return LiveFill(
            order_id=order_id,
            market_id=market.market_id,
            side=side,
            avg_price=avg_price,
            size_usd=size_usd,
            shares=shares,
            fee_usd=fee_usd,
            raw_response=resp,
        )

    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancels a single open order by its orderID (the same field the CLOB
        returns in the POST /order response). Returns True on success, False
        on failure. Errors are caught and logged with the real message rather
        than raised — never silently swallowed.
        """
        try:
            resp = self._client.cancel_order(payload=OrderPayload(orderID=order_id))
        except Exception as exc:
            logger.error("[LIVE] cancel_order(%s) failed: %s", order_id, exc)
            return False

        if not isinstance(resp, dict):
            logger.error("[LIVE] cancel_order(%s) unexpected response: %s", order_id, resp)
            return False

        canceled = resp.get("canceled")
        if not isinstance(canceled, list):
            logger.error("[LIVE] cancel_order(%s) unexpected response: %s", order_id, resp)
            return False
        if order_id in canceled:
            logger.info("[LIVE] cancel_order(%s) success", order_id)
            return True

        not_canceled = resp.get("not_canceled") or {}
        if not isinstance(not_canceled, dict):
            not_canceled = {}
        reason = not_canceled.get(order_id, "not present in canceled list")
        logger.warning("[LIVE] cancel_order(%s) not cancelled: %s", order_id, reason)
        return False

    async def cancel_all_orders(self, market_id: str | None = None) -> int:
        """
        Cancels all open orders for the account, or — when market_id is given
        — only the orders for that market (real CLOB cancel-all / cancel-
        market-orders endpoints). Returns the number of orders successfully
        cancelled. Errors are caught and logged rather than raised.
        """
        try:
            if market_id is not None:
                resp = self._client.cancel_market_orders(
                    payload=OrderMarketCancelParams(market=market_id)
                )
            else:
                resp = self._client.cancel_all()
        except Exception as exc:
            logger.error("[LIVE] cancel_all_orders(market_id=%s) failed: %s", market_id, exc)
            return 0

        canceled = resp.get("canceled") if isinstance(resp, dict) else None
        if not isinstance(canceled, list):
            logger.error(
                "[LIVE] cancel_all_orders(market_id=%s) unexpected response: %s",
                market_id, resp,
            )
            return 0

        count = len(canceled)
        logger.info(
            "[LIVE] cancel_all_orders(market_id=%s) cancelled %d order(s)",
            market_id, count,
        )
        return count

    async def get_open_positions(self, wallet_address: str) -> list[dict]:
        """
        Read-only: current positions from the public Data API
        (GET https://data-api.polymarket.com/positions?user=<address>).
        No auth required for this read.
        """
        resp = await self._data_client.get("/positions", params={"user": wallet_address})
        resp.raise_for_status()
        return resp.json()

    async def redeem_position(self, market: Market, trade: dict) -> str:
        """
        Redeem resolved outcome tokens for `market` back into pUSD on-chain.

        Calls the exact function and contract verified in Step 3.1 (docs +
        Polygonscan verified ABI):

            CtfCollateralAdapter.redeemPositions(
                address collateralToken,   // pUSD
                bytes32 parentCollectionId, // 32 zero bytes for top-level markets
                bytes32 conditionId,        // the market's Gamma conditionId
                uint256[] indexSets,        // [1, 2] — both outcomes
            )

        at 0xAdA100Db00Ca00073811820692005400218FcE1f, paying out in pUSD
        (0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB). Returns the transaction
        hash as a hex string.

        Wired into main.py's settlement loop for resolved LIVE positions and
        into scripts/manual_redeem.py for one-off manual redemption (the
        broadcast logic itself is shared from engine/redeem.py). The caller
        must supply a Market whose market_id is
        the Gamma conditionId (0x-prefixed hex) and a DB trade row (used for
        logging context). Requires POLYGON_RPC_URL in the environment (no
        default — see scripts/benchmark_rpc.py). Per the docs, the wallet must
        have approved this adapter via setApprovalForAll on the Conditional
        Tokens contract; that approval step is the caller's responsibility.

        Raises on any RPC/signing error — this moves real funds, so a failure
        must be loud, not silently swallowed.
        """
        rpc_url = os.environ.get("POLYGON_RPC_URL", "")
        if not rpc_url:
            raise RuntimeError(
                "POLYGON_RPC_URL is not set — required for on-chain redemption. "
                "Pick it based on a measurement (see scripts/benchmark_rpc.py)."
            )

        # Format validation of condition_id lives inside broadcast_redeem_tx
        # (single source of truth — it raises ValueError for anything that
        # isn't a 0x-prefixed 32-byte conditionId).
        condition_id = market.market_id

        # The build/sign/broadcast itself lives in engine/redeem.py
        # (broadcast_redeem_tx) — the verified, settings-free primitive shared
        # with scripts/manual_redeem.py.
        tx_hash_hex = broadcast_redeem_tx(
            private_key=self._private_key,
            condition_id=condition_id,
            rpc_url=rpc_url,
        )

        logger.info(
            "[LIVE] Redeem submitted: market %s trade %s conditionId %s -> tx %s",
            market.market_id, trade.get("id"), condition_id, tx_hash_hex,
        )
        return tx_hash_hex

    async def settle_position(self, market: Market):
        """
        NOT IMPLEMENTED — deliberately. The on-chain redemption primitive
        (redeem_position(), verified against docs + Polygonscan in Step 3.1)
        IS wired into main.py's settlement loop for resolved live positions.
        This method — a distinct "decide which positions to redeem, check
        approvals, drive the flow" abstraction — remains unimplemented so
        there is exactly one well-tested redemption path rather than two
        competing ones. The raise is intentional: nothing should call this.
        """
        raise NotImplementedError(
            "LiveBroker.settle_position is intentionally not implemented. The "
            "redemption primitive redeem_position() exists and is verified, but "
            "the full settle flow (which positions to redeem, approval checks, "
            "wiring into the loop) is deliberately left for explicit opt-in."
        )

    async def aclose(self) -> None:
        await self._data_client.aclose()


def _all_gate_conditions_met(settings: Settings) -> bool:
    return (
        settings.LIVE_TRADING_CONFIRMED_1
        and settings.LIVE_TRADING_CONFIRMED_2
        and settings.LIVE_TRADING_CONFIRMED_3
        and settings.PAPER_MODE is False
    )


async def build_live_broker(settings: Settings, alerter: TelegramAlerter) -> LiveBroker:
    """
    The ONLY way to obtain a LiveBroker instance. Unchanged in spirit from the
    original scaffold:

    1. Reads POLYGON_PRIVATE_KEY strictly from os.environ.
    2. Refuses to instantiate unless ALL THREE LIVE_TRADING_CONFIRMED_* flags
       are True AND PAPER_MODE is explicitly False.
    3. Refuses to instantiate without POLYGON_RPC_URL — the settlement loop's
       redeem_position() cannot run without it, and a live bot that can never
       settle is a broken configuration. Raises loudly (not the gate error)
       so get_broker() can't silently fall back to paper mode.
    4. On success, sends a "LIVE TRADING ENABLED" Telegram alert immediately.
    """
    if not _all_gate_conditions_met(settings):
        raise LiveTradingNotEnabledError(
            "Refusing to build a live broker: not all gate conditions are met. "
            "Requires LIVE_TRADING_CONFIRMED_1=True, LIVE_TRADING_CONFIRMED_2=True, "
            "LIVE_TRADING_CONFIRMED_3=True, and PAPER_MODE=False, all simultaneously."
        )

    private_key = os.environ.get("POLYGON_PRIVATE_KEY", "")
    if not private_key:
        raise LiveTradingNotEnabledError(
            "All confirmation flags are set but POLYGON_PRIVATE_KEY is empty in "
            "the environment. Refusing to proceed without it."
        )

    # A live broker that opens real positions but can never settle them is a
    # broken configuration: redeem_position() hard-requires the RPC URL.
    # Deliberately NOT a LiveTradingNotEnabledError so get_broker()'s gate
    # fallback can't silently downgrade this to paper mode — it must be loud.
    if not os.environ.get("POLYGON_RPC_URL", ""):
        raise RuntimeError(
            "All confirmation flags are set and POLYGON_PRIVATE_KEY is present, "
            "but POLYGON_RPC_URL is missing. Live settlement (redeem_position) "
            "cannot run without it — pick one via scripts/benchmark_rpc.py. "
            "Refusing to start live trading in a state that can never settle."
        )

    signature_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "1"))
    broker = LiveBroker(private_key=private_key, alerter=alerter, signature_type=signature_type)

    timestamp = datetime.now(timezone.utc).isoformat()
    await alerter.send_alert(
        f"LIVE TRADING ENABLED at {timestamp}. All three confirmation flags "
        f"were set and PAPER_MODE=False. If this wasn't you, revoke the "
        f"private key immediately.",
        level=AlertLevel.CRITICAL,
    )
    logger.critical("LIVE TRADING ENABLED at %s", timestamp)

    return broker
