"""Live trading strategy loop: two-consecutive-green entry, 3-bar hold.

Extracted from ``app.services.bot_runner`` (FINDING A — file-size cap).
The registry's ``_supervise`` method delegates to :func:`run_trade_bot`
when ``binding.mode == "trade"``; the runner itself stays below 800 lines.
"""

from __future__ import annotations

import datetime
import logging
import zoneinfo

from app.broker.alpaca.clerk.clerk import get_alpaca_clerk
from app.broker.contract.models import BrokerOrderLeg, OrderSide
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.marketdata.feed import MarketDataFeed

# The imports above (get_alpaca_clerk, BrokerOrderLeg, session_window_for_date)
# were deferred in bot_runner.py to avoid an import cycle with the registry
# module's own graph.  No such cycle exists from this leaf module — verified:
# app.broker.alpaca.clerk.clerk does not import from app.services.*, and
# app.lean_sidecar.trading_calendar does not import from app.services.*.

# Module-level ET zone constant (FINDING B — hoist out of per-bar loop).
_ET = zoneinfo.ZoneInfo("America/New_York")

logger = logging.getLogger(__name__)


def _log_decision(sid: str, decision: str, bar, broker: str) -> None:
    """Emit the 7-field ``bot_decision`` structured log (FINDING C).

    Called at every decision point; keeps the per-site call to one line.
    ``bar`` is a :class:`~app.marketdata.feed.MarketDataBar`.
    """
    logger.info(
        "Trade bot decision",
        extra={
            "action": "bot_decision",
            "strategy_instance_id": sid,
            "decision": decision,
            "symbol": bar.symbol,
            "bar_start_ms": bar.start_ms,
            "bar_end_ms": bar.end_ms,
        },
    )


async def run_trade_bot(binding, feed: MarketDataFeed) -> None:
    """Live trading bot: two-consecutive-green entry, 3-bar hold, clerk submit.

    Deliberate divergences from DeploymentValidationConsecutiveGreen (numerical-rigor rule 4):

    1. **Fixed-share sizing**: uses ``binding.quantity`` shares (market order)
       instead of ``set_holdings(1.0)`` because there is no cash/portfolio
       context in the live clerk path. The engine reference targets 100% equity
       allocation via a portfolio-cash model; we approximate with a fixed share
       count, which is documented and the only viable live-order equivalent.

    2. **3-bar hold counted from SUBMIT (bar index), not from fill event**:
       the engine reference resets ``_bars_until_exit_signal`` in
       ``on_order_event`` (which fires asynchronously on the fill). Here we
       set the counter immediately when the BUY is submitted, assuming
       immediate market-order fill. A live fill that arrives late does not
       shift the exit bar — that drift is acceptable and documented.

    Detection window: [session_open + 15 min, session_close − 15 min) derived
    from the canonical calendar module (``app.lean_sidecar.trading_calendar``).
    No hardcoded ``time(9,45)``/``time(15,45)`` literals — temporal-rigor ban.

    Order attribution: ``clerk.submit_for_instance(strategy_instance_id=sid, ...)``.
    The bot namespace is ``learn-ai/{sid}/v1`` so the panel's
    ``project_instance_fills`` fold picks up every fill this bot submits.

    Stop (CancelledError) never submits orders — the cancellation guard is
    explicit. Exposure is left open intentionally (panel-locked decision: stop
    keeps exposure).
    """
    sid = binding.strategy_instance_id
    quantity = binding.quantity
    logger.info(
        "Trade bot on duty",
        extra={
            "action": "bot_on_duty",
            "strategy_instance_id": sid,
            "broker": binding.broker,
            "symbol": binding.symbol,
            "run_id": binding.run_id,
            "mode": "trade",
            "quantity": quantity,
        },
    )

    # Per-session mutable state — all resets on day rollover.
    green_streak: int = 0
    in_position: bool = False
    bars_since_entry: int = 0
    window_end_flatten_submitted: bool = False
    current_date_open_ms: int | None = None
    current_date_close_ms: int | None = None

    _WINDOW_OFFSET_MS = 15 * 60 * 1_000  # 15 minutes in ms

    async for bar in feed.stream_bars(binding.symbol, use_rth=binding.use_rth):
        # bar.end_ms is the bar-close timestamp (int64 ms UTC).
        bar_ms = bar.end_ms

        # Derive the session window from the canonical calendar module.
        # ZoneInfo conversion is transient — result not persisted.
        bar_ny_date = datetime.datetime.fromtimestamp(bar_ms / 1000, tz=_ET).date()

        # On day rollover, refresh the session window and reset day state.
        try:
            session = session_window_for_date(bar_ny_date)
            session_open_ms = session.open_ms_utc
            session_close_ms = session.close_ms_utc
        except LookupError:
            # Not a session day — skip bar, reset streak.
            green_streak = 0
            continue

        if (current_date_open_ms, current_date_close_ms) != (session_open_ms, session_close_ms):
            current_date_open_ms = session_open_ms
            current_date_close_ms = session_close_ms
            green_streak = 0
            window_end_flatten_submitted = False

        window_start_ms = session_open_ms + _WINDOW_OFFSET_MS
        window_end_ms = session_close_ms - _WINDOW_OFFSET_MS

        decision = "HOLD"

        # ── Window-end: flatten if holding ───────────────────────────────
        if bar_ms >= window_end_ms:
            if in_position and not window_end_flatten_submitted:
                clerk = get_alpaca_clerk()
                if clerk is None:
                    raise RuntimeError(
                        "AlpacaClerk is not installed; cannot submit FLATTEN_WINDOW_END order."
                    )
                decision = "FLATTEN_WINDOW_END"
                _log_decision(sid, decision, bar, binding.broker)
                sell_leg = BrokerOrderLeg(symbol=binding.symbol, side=OrderSide.SELL, quantity=float(quantity))
                result = await clerk.submit_for_instance(
                    strategy_instance_id=sid, legs=[sell_leg]
                )
                window_end_flatten_submitted = True
                in_position = False
                bars_since_entry = 0
                green_streak = 0
                logger.info(
                    "Trade bot FLATTEN_WINDOW_END submitted",
                    extra={
                        "action": "bot_order_submitted",
                        "strategy_instance_id": sid,
                        "side": "sell",
                        "quantity": quantity,
                        "order_ref": result.results[0].order_ref if result.results else None,
                    },
                )
            else:
                _log_decision(sid, "HOLD", bar, binding.broker)
            continue

        # ── Before detection window: no entries ───────────────────────────
        if bar_ms < window_start_ms:
            green_streak = 0
            _log_decision(sid, "HOLD", bar, binding.broker)
            continue

        # ── Inside detection window ───────────────────────────────────────
        if in_position:
            bars_since_entry += 1
            if bars_since_entry >= 3:
                clerk = get_alpaca_clerk()
                if clerk is None:
                    raise RuntimeError(
                        "AlpacaClerk is not installed; cannot submit EXIT order."
                    )
                decision = "EXIT"
                _log_decision(sid, decision, bar, binding.broker)
                sell_leg = BrokerOrderLeg(symbol=binding.symbol, side=OrderSide.SELL, quantity=float(quantity))
                result = await clerk.submit_for_instance(
                    strategy_instance_id=sid, legs=[sell_leg]
                )
                in_position = False
                bars_since_entry = 0
                green_streak = 0
                logger.info(
                    "Trade bot EXIT submitted",
                    extra={
                        "action": "bot_order_submitted",
                        "strategy_instance_id": sid,
                        "side": "sell",
                        "quantity": quantity,
                        "order_ref": result.results[0].order_ref if result.results else None,
                    },
                )
            else:
                _log_decision(sid, "HOLD", bar, binding.broker)
            continue

        # Not in position: update green streak.
        if bar.close > bar.open:
            green_streak += 1
        else:
            green_streak = 0

        if green_streak >= 2:
            clerk = get_alpaca_clerk()
            if clerk is None:
                raise RuntimeError(
                    "AlpacaClerk is not installed; cannot submit ENTER order."
                )
            decision = "ENTER"
            _log_decision(sid, decision, bar, binding.broker)
            buy_leg = BrokerOrderLeg(symbol=binding.symbol, side=OrderSide.BUY, quantity=float(quantity))
            result = await clerk.submit_for_instance(
                strategy_instance_id=sid, legs=[buy_leg]
            )
            in_position = True
            bars_since_entry = 0
            green_streak = 0
            logger.info(
                "Trade bot ENTER submitted",
                extra={
                    "action": "bot_order_submitted",
                    "strategy_instance_id": sid,
                    "side": "buy",
                    "quantity": quantity,
                    "order_ref": result.results[0].order_ref if result.results else None,
                },
            )
        else:
            _log_decision(sid, "HOLD", bar, binding.broker)
