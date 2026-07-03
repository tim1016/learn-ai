"""VCR-0002 production-wiring fix — LiveEngine.run() threads
intent_wal_path through to LivePortfolio.

Phase 5B (commit aae1cf2c) added a fail-fast in
``LivePortfolio.__post_init__``: a real-broker adapter (with
``requires_durable_submit=True``) without an ``intent_wal`` raises
``ValueError("ADR 0008 / Phase 5B: ...")``.

But the production ``run.cmd_start → LiveEngine`` path never threaded
``intent_wal_path`` through to ``LivePortfolio``, so the engine
crashed on construction in production any time a real broker was used.

This was discovered live during the 2026-06-16 HITL deployment-
validation paper deploy: the first engine subprocess died with the
Phase 5B ValueError after 5.7 seconds. An in-place patch on the host
venv unblocked the HITL run; this PR makes the fix durable.

The patch:
  * LiveEngine.run() constructs IntentWal + bot_order_namespace from
    intent_wal_path + strategy_instance_id when both are set, and
    passes them to LivePortfolio.
  * run.cmd_start passes
    ``intent_wal_path=args.run_dir / "intent_events.jsonl"`` to
    LiveEngine.

Tests below pin the wired path: a LiveEngine with intent_wal_path set
constructs LivePortfolio successfully against a real-broker fake.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.ibkr.account_truth_freshness import ACCOUNT_TRUTH_SOURCE_FRESHNESS_SPECS
from app.broker.ibkr.models import IbkrConnectionHealth
from app.engine.data.trade_bar import TradeBar
from app.engine.live.config import LiveConfig
from app.engine.live.live_engine import LiveEngine
from app.engine.strategy.base import Strategy
from app.schemas.account_truth import AccountTruthResponse, AccountTruthSourceFreshness
from app.services.account_truth_snapshot import get_account_truth_snapshot_provider
from app.utils.timestamps import now_ms_utc
from tests.engine.live.fixtures.fake_broker import FakeBroker, iter_bars


class _RealBrokerFake(FakeBroker):
    """FakeBroker subclass that declares itself a real-broker adapter so
    the Phase 5B fail-fast in ``LivePortfolio.__post_init__`` fires.
    Mirrors the fixture used by the existing intent-identity wiring
    tests in ``test_intent_identity_wiring.py``."""

    requires_durable_submit = True


class _NoopStrategy(Strategy):
    def initialize(self) -> None:
        assert self.ctx is not None
        self.ctx.add_equity("SPY")
        self.ctx.register_consolidator("SPY", timedelta(minutes=1), self.on_bar)

    def on_bar(self, bar: TradeBar) -> None:
        return


def _bar(minute: int) -> TradeBar:
    start = datetime(2026, 5, 4, 14, 0, tzinfo=UTC) + timedelta(minutes=minute)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal("500"),
        high=Decimal("500"),
        low=Decimal("500"),
        close=Decimal("500"),
        volume=100,
    )


def _remember_clean_account_truth() -> None:
    generated_at_ms = now_ms_utc()
    get_account_truth_snapshot_provider().remember(
        AccountTruthResponse(
            account_id="DU123",
            final_verdict="clean",
            final_severity="ok",
            status_label="Clean",
            status_detail="Account Truth is clean.",
            generated_at_ms=generated_at_ms,
            health=IbkrConnectionHealth(
                mode="paper",
                host="127.0.0.1",
                port=4002,
                client_id=7,
                connected=True,
                account_id="DU123",
                is_paper=True,
                fetched_at_ms=generated_at_ms,
                connection_state="connected",
                last_transition_ms=generated_at_ms,
            ),
            invariants=[],
            source_freshness=_fresh_source_freshness(generated_at_ms),
        ),
        cached_at_ms=generated_at_ms,
    )


def _fresh_source_freshness(generated_at_ms: int) -> list[AccountTruthSourceFreshness]:
    return [
        AccountTruthSourceFreshness(
            source=spec.source,
            label=spec.label,
            status="fresh",
            severity=spec.severity,
            fetched_at_ms=generated_at_ms,
            age_ms=0,
            hard_ttl_ms=spec.hard_ttl_ms,
            reason_code=None,
            message=f"{spec.label} evidence is fresh.",
        )
        for spec in ACCOUNT_TRUTH_SOURCE_FRESHNESS_SPECS
    ]


@pytest.mark.asyncio
async def test_engine_constructs_portfolio_with_intent_wal_against_real_broker(
    tmp_path: Path,
) -> None:
    """The production-wiring fix: a LiveEngine constructed with
    ``intent_wal_path`` AND ``strategy_instance_id`` MUST be able to
    construct a LivePortfolio against a real-broker adapter without
    tripping the Phase 5B fail-fast. Without the patch, this test
    raises ``ValueError("ADR 0008 / Phase 5B: ...")`` during
    ``engine.run()``."""
    broker = _RealBrokerFake()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        intent_wal_path=tmp_path / "intent_events.jsonl",
        strategy_instance_id="dep_val_smoke_test",
    )

    # If the wiring fix isn't in place, engine.run() raises during
    # LivePortfolio construction with:
    #   ValueError: ADR 0008 / Phase 5B: LivePortfolio with a real-broker
    #   adapter (IbkrBrokerAdapter) cannot be constructed without an
    #   IntentWal.
    # The test passes iff engine.run() completes without raising — the
    # wire-through happened and Phase 5B's structural check was
    # satisfied.
    _remember_clean_account_truth()
    try:
        await engine.run(_NoopStrategy(), iter_bars([_bar(m) for m in range(3)]))
    finally:
        get_account_truth_snapshot_provider().clear()


@pytest.mark.asyncio
async def test_engine_without_intent_wal_path_still_works_with_fake_broker(
    tmp_path: Path,
) -> None:
    """Backward-compat: engines constructed without ``intent_wal_path``
    continue to work against a FakeBroker (``requires_durable_submit
    = False``). The fix only fires when the path is provided AND the
    strategy_instance_id is non-empty — pre-existing replay /
    shadow-mode tests are unaffected."""
    broker = FakeBroker()  # NOT a real-broker fake
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        # intent_wal_path omitted — pre-Phase-5B path
    )

    await engine.run(_NoopStrategy(), iter_bars([_bar(m) for m in range(3)]))

    # The IntentWal path was never set, so no jsonl is written.
    assert not (tmp_path / "intent_events.jsonl").exists()


@pytest.mark.asyncio
async def test_engine_with_intent_wal_path_but_no_instance_id_falls_back_to_fake_broker(
    tmp_path: Path,
) -> None:
    """Defensive: ``intent_wal_path`` alone doesn't activate the
    durable-submit wiring — the engine also needs a non-empty
    ``strategy_instance_id`` to compute the namespace. With one but
    not the other, the engine falls back to the legacy LivePortfolio
    construction (no intent_wal injected), so it works against
    FakeBroker but would fail against a real-broker fake.

    This is intentional: the fix opens the wiring only when both
    inputs are present, mirroring how the production cmd_start path
    populates them together."""
    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        intent_wal_path=tmp_path / "intent_events.jsonl",
        # strategy_instance_id omitted (defaults to "")
    )

    await engine.run(_NoopStrategy(), iter_bars([_bar(m) for m in range(3)]))
    # No IntentWal injected → no file written, but no crash either.
    assert not (tmp_path / "intent_events.jsonl").exists()
