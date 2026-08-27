"""Market-liveness test support shared by the bot_runner test package and
outside suites that exercise ``BotTaskRegistry`` / ``run_trade_bot`` end to
end.

Split out of ``tests/services/bot_runner/conftest.py`` (issue #1810): the
review found that file acting as an undeclared public library, with an
autouse fixture that outside modules imported purely for its registration
side effect. ``patch_fresh_live_market_liveness`` is the extracted
implementation -- every consumer (the bot_runner package's own autouse
fixture, and each outside module's own explicit autouse fixture) calls it
directly instead of importing a fixture to trigger it by side effect.
"""

from __future__ import annotations

import pytest

import app.broker.alpaca.clerk.sqlite.runtime as clerk_runtime
import app.services.bot_runner as bot_runner
import app.services.bot_trade_strategy as bot_trade_strategy
from app.schemas.market_liveness import (
    MarketClockLivenessEvidence,
    SymbolTradingStatusEvidence,
)
from app.schemas.run_admission import StrategyValidationAdmissionFact
from app.services.market_liveness import compose_market_liveness


def _tradable_market_liveness(symbol: str, observed_at_ms: int):
    return compose_market_liveness(
        symbol,
        now_ms=observed_at_ms,
        market_clock=MarketClockLivenessEvidence(
            state="OPEN",
            source="test.clock",
            observed_at_ms=observed_at_ms,
            vendor_timestamp_ms=observed_at_ms,
        ),
        connected=True,
        connection_changed_at_ms=observed_at_ms,
        symbol_status=SymbolTradingStatusEvidence(
            symbol=symbol,
            state="TRADABLE",
            source="test.symbol-status",
            observed_at_ms=observed_at_ms,
            source_timestamp_ms=observed_at_ms,
        ),
    )


def _verified_validation_fact(_binding: object, observed_at_ms: int) -> StrategyValidationAdmissionFact:
    """Keep runner tests focused on task/custody behavior, not manifest fixtures."""
    return StrategyValidationAdmissionFact(
        state="VERIFIED",
        strategy_key="deployment_validation",
        evidence_status="accepted",
        event_id="test-validation-event",
        evidence_snapshot_sha256="a" * 64,
        verified_at_ms=observed_at_ms,
        explanation="Test validation evidence is current.",
    )


def patch_fresh_live_market_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch every module-level ``market_liveness_fact`` binding (and the
    strategy-validation admission fact) the bot_runner code paths read, so
    a test starts with every symbol live/tradable and validation VERIFIED.

    Callers wrap this in their own ``@pytest.fixture(autouse=True)`` --
    registration is explicit at each call site rather than an import-only
    side effect (issue #1810).
    """
    monkeypatch.setattr(bot_runner, "market_liveness_fact", _tradable_market_liveness)
    # #1671: the Clerk's own submission-boundary recheck (runtime.py) reads
    # this module's import of the same name -- a separate binding from
    # bot_trade_strategy's, so it needs its own patch or it falls through to
    # the real (unconfigured, fail-closed) store and every ENTER is rejected.
    monkeypatch.setattr(
        bot_trade_strategy,
        "market_liveness_fact",
        _tradable_market_liveness,
    )
    monkeypatch.setattr(clerk_runtime, "market_liveness_fact", _tradable_market_liveness)
    monkeypatch.setattr(bot_runner, "current_strategy_validation_fact", _verified_validation_fact)
