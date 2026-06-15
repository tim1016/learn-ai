"""Phase 5C cancel-confirm timeout / VCR-0002.

PRD §5C step 4-5: every managed cancel/flatten path follows cancel →
wait for confirms → fetch positions → liquidate. When the broker can't
confirm cancels within ``CANCEL_CONFIRM_TIMEOUT_S``, the engine writes
``halt.flag`` (CANCEL_CONFIRM_TIMEOUT_HALT) and refuses to liquidate —
submitting market liquidations against a broker we can't confirm
cancel state with would race the just-issued cancels.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.live.config import LiveConfig
from app.engine.live.live_engine import (
    CancelConfirmTimeoutHaltError,
    LiveEngine,
)
from tests.engine.live.fixtures.fake_broker import FakeBroker


class _HangingCancelBroker(FakeBroker):
    """Cancel_open_orders awaits forever — exercises the timeout path."""

    async def cancel_open_orders(self) -> list[int]:
        await asyncio.sleep(10.0)
        return []


@pytest.mark.asyncio
async def test_flatten_halts_on_cancel_confirm_timeout_vcr_0002(
    tmp_path: Path,
) -> None:
    """The load-bearing safety: a flatten triggered while the broker
    can't confirm cancels writes halt.flag and raises rather than
    liquidating into the uncertain cancel state."""
    broker = _HangingCancelBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        cancel_confirm_timeout_s=0.05,  # short timeout for the test
    )
    # Direct exercise of the _flatten path with a hanging cancel.
    from app.engine.live.live_portfolio import LivePortfolio

    portfolio = LivePortfolio(broker)
    portfolio.update_reference_price("SPY", Decimal("500"))
    ctx = type("ctx", (), {"log": lambda self_, msg: None})()

    with pytest.raises(CancelConfirmTimeoutHaltError) as exc:
        await engine._flatten(portfolio, ctx, bar_time=datetime(2026, 5, 4, 14, 30, tzinfo=UTC))  # type: ignore[arg-type]

    assert exc.value.timeout_s == 0.05
    # halt.flag carries the timeout marker so the failure list can label it.
    halt_path = tmp_path / "halt.flag"
    assert halt_path.exists()
    payload = halt_path.read_text(encoding="utf-8")
    assert "CANCEL_CONFIRM_TIMEOUT_HALT" in payload
    assert "path=_flatten" in payload
    # No liquidation orders may be submitted — the broker stays untouched.
    assert broker.orders == []


@pytest.mark.asyncio
async def test_flatten_proceeds_when_cancel_completes_in_time(
    tmp_path: Path,
) -> None:
    """The fast path: a broker that confirms cancels within the window
    proceeds to liquidation as before. Verifies the timeout isn't
    overzealous."""
    broker = FakeBroker()  # cancel_open_orders returns immediately
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        cancel_confirm_timeout_s=0.5,
    )
    from app.engine.live.live_portfolio import LivePortfolio

    portfolio = LivePortfolio(broker)
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.get_position("SPY").quantity = 100  # something to liquidate
    ctx = type("ctx", (), {"log": lambda self_, msg: None})()

    acks = await engine._flatten(portfolio, ctx, bar_time=datetime(2026, 5, 4, 14, 30, tzinfo=UTC))  # type: ignore[arg-type]

    # One liquidation order submitted; halt.flag NOT written.
    assert len(acks) == 1
    assert not (tmp_path / "halt.flag").exists()


@pytest.mark.asyncio
async def test_flatten_tolerates_cancel_exception_without_halting(
    tmp_path: Path,
) -> None:
    """A non-timeout exception in cancel_open_orders (network blip,
    transient) preserves the prior tolerant behavior: log + continue to
    liquidate. PRD §5C is silent on this branch; only the timeout path
    is load-bearing safety."""

    class _RaisingCancelBroker(FakeBroker):
        async def cancel_open_orders(self) -> list[int]:
            raise RuntimeError("transient broker error")

    broker = _RaisingCancelBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        cancel_confirm_timeout_s=0.5,
    )
    from app.engine.live.live_portfolio import LivePortfolio

    portfolio = LivePortfolio(broker)
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.get_position("SPY").quantity = 100
    ctx = type("ctx", (), {"log": lambda self_, msg: None})()

    acks = await engine._flatten(portfolio, ctx, bar_time=datetime(2026, 5, 4, 14, 30, tzinfo=UTC))  # type: ignore[arg-type]

    assert len(acks) == 1  # liquidation still ran
    assert not (tmp_path / "halt.flag").exists()
