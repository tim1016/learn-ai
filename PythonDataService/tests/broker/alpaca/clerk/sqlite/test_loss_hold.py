"""The ADR 0059 D4 loss hold as a registered account-hold cause.

The hold refuses new exposure account-wide and still admits every reduction,
so each program keeps managing the position it already holds. These tests
drive the behaviour through the same seams production does — ``decide_capability``,
``raise_account_hold`` and ``resolve_account_hold`` — rather than reading the
stored envelope back.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.live_envelope import ENVELOPE_ADMISSION_REASON_CODES
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    Capability,
    ReductionIntent,
    RefusalClass,
    classify_admission_refusal,
    decide_capability,
    raise_account_hold,
    resolve_account_hold,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    HOLD_REASON_CODES,
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
    STREAM_HEALTH_HOLD_REASON_CODE,
    LossHoldCause,
)
from tests.broker.alpaca.clerk.sqlite.conftest import _clock_at, _make_held_position

ACCOUNT_ID = "PA-LOSS"
SID = "loss-bot"
RUN_ID = "run-1"

CAUSE = LossHoldCause(
    day_start_ms=1_788_000_000_000,
    day_pnl_usd=-5_250.0,
    loss_limit_usd=5_000.0,
    last_equity_usd=100_000.0,
    observed_at_ms=1_788_040_000_000,
)


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[ClerkSqliteRepository]:
    clerk = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_at(1_700_000_000_000)
    )
    clerk.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    yield clerk
    clerk.close()


@pytest.fixture
async def registered_long(repo: ClerkSqliteRepository) -> tuple[str, ReductionIntent]:
    """A registered instance holding a proved long, plus a reduction of it."""
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    await _make_held_position(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, run_id=RUN_ID)
    assert repo.position(SID, "SPY") > 0
    return SID, ReductionIntent(symbol="SPY", side="SELL", quantity=1)


def test_the_cause_round_trips_and_refuses_partial_or_non_finite_facts() -> None:
    assert LossHoldCause.from_mapping(CAUSE.to_mapping()) == CAUSE
    with pytest.raises(ValueError):
        LossHoldCause.from_mapping({**CAUSE.to_mapping(), "day_pnl_usd": float("nan")})
    with pytest.raises(ValueError):
        LossHoldCause.from_mapping(
            {k: v for k, v in CAUSE.to_mapping().items() if k != "observed_at_ms"}
        )


def test_the_loss_hold_is_a_hold_cause_and_every_envelope_refusal_is_transient() -> None:
    assert LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE in HOLD_REASON_CODES
    for code in ENVELOPE_ADMISSION_REASON_CODES:
        assert classify_admission_refusal(code) is RefusalClass.TRANSIENT


async def test_a_raised_loss_hold_blocks_new_exposure_and_admits_every_reduction(
    repo: ClerkSqliteRepository, registered_long: tuple[str, ReductionIntent]
) -> None:
    strategy_instance_id, intent = registered_long
    outcome = raise_account_hold(
        repo,
        reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
        evidence_refs=[f"day-pnl:{CAUSE.day_start_ms}"],
        cause_facts=CAUSE.to_mapping(),
    )
    assert outcome == "raised"

    entry = decide_capability(
        repo, capability=Capability.NEW_EXPOSURE, strategy_instance_id=strategy_instance_id
    )
    assert not entry.allowed and entry.reason_code == LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE

    reduce = decide_capability(
        repo,
        capability=Capability.REDUCE,
        strategy_instance_id=strategy_instance_id,
        reduction_intent=intent,
    )
    assert reduce.allowed


def test_a_hold_that_forbids_reduction_still_refuses_it(repo: ClerkSqliteRepository) -> None:
    """The loss hold is the exception, not a new rule for every hold.

    ``decide_capability`` no longer short-circuits REDUCE for *any* active
    hold — it consults each hold's registered policy first. A hold that
    declares ``allows_reduction=False`` must still refuse, by whichever path
    it reaches the decision.
    """
    raise_account_hold(
        repo,
        reason_code=STREAM_HEALTH_HOLD_REASON_CODE,
        evidence_refs=["trades: no message for 90s"],
    )

    decision = decide_capability(
        repo,
        capability=Capability.REDUCE,
        strategy_instance_id=SID,
        reduction_intent=ReductionIntent(symbol="SPY", side="SELL", quantity=1),
    )
    assert not decision.allowed and decision.reason_code == STREAM_HEALTH_HOLD_REASON_CODE


def test_an_unchanged_raise_appends_nothing_and_the_clear_resolves_it(
    repo: ClerkSqliteRepository,
) -> None:
    raise_account_hold(
        repo,
        reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
        evidence_refs=["day-pnl:1"],
        cause_facts=CAUSE.to_mapping(),
    )
    before = repo.control_meta_snapshot().control_revision

    assert (
        raise_account_hold(
            repo,
            reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
            evidence_refs=["day-pnl:1"],
            cause_facts=CAUSE.to_mapping(),
        )
        == "unchanged"
    )
    assert repo.control_meta_snapshot().control_revision == before

    assert resolve_account_hold(
        repo,
        reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
        summary_code="LIVE_ENVELOPE_LOSS_HOLD_CLEARED",
    )
    assert (
        repo.active_uncertainty(
            scope="ACCOUNT_CLERK",
            reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
            strategy_instance_id=None,
        )
        is None
    )


def test_raising_the_loss_hold_without_its_facts_is_a_programming_error(
    repo: ClerkSqliteRepository,
) -> None:
    with pytest.raises(ValueError, match="cause_facts"):
        raise_account_hold(
            repo,
            reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
            evidence_refs=["day-pnl:1"],
        )
