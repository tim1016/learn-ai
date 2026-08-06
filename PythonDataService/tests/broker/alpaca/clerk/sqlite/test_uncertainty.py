"""Typed uncertainty envelope and admission tests (#1380, Part A).

Covers: BOT uncertainty blocks only the affected bot; ACCOUNT_CLERK blocks
every bot; unrecognized/default-shaped uncertainties fail closed; admission
is one function checking both uncertainties and the #1378 hold mechanism;
raise/resolve are idempotent.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite.facts import AccountHoldRaisedFacts
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    Capability,
    ReductionIntent,
    admit_new_exposure,
    decide_capability,
    raise_uncertainty,
    require_admission,
    resolve_reconciliation_uncertainty,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    OrderOutcomeUnknownCause,
    PositionDriftCause,
    PositionDriftObservation,
    UnknownOrderIdentity,
)

ACCOUNT_ID = "PA-TEST"
SID = "spy-bot"
OTHER_SID = "qqq-bot"


def _clock_seq() -> Callable[[], int]:
    counter = {"t": 1_700_000_000_000}

    def clock() -> int:
        counter["t"] += 1
        return counter["t"]

    return clock


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[ClerkSqliteRepository]:
    r = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    r.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    r.register_strategy_instance(strategy_instance_id=OTHER_SID, symbol="QQQ", config_hash="h2")
    yield r
    r.close()


def _raise(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str | None,
    **overrides: Any,
) -> bool:
    kwargs: dict[str, Any] = {
        "reason_code": "ORDER_OUTCOME_UNKNOWN",
        "headline": "headline",
        "explanation": "explanation",
        "operator_impact": "operator impact",
        "next_step": "next step",
    }
    kwargs.update(overrides)
    return raise_uncertainty(repo, strategy_instance_id=strategy_instance_id, **kwargs)


def test_cause_encoders_emit_the_unique_sorted_order_required_by_decoders() -> None:
    position_cause = PositionDriftCause(
        positions=(
            PositionDriftObservation(symbol="SPY", broker_qty=2.0, attributed_qty=1.0),
            PositionDriftObservation(symbol="QQQ", broker_qty=3.0, attributed_qty=1.0),
        )
    )
    order_cause = OrderOutcomeUnknownCause(
        identities=(
            UnknownOrderIdentity(effect_operation_id="effect:b", order_ref="order:2"),
            UnknownOrderIdentity(effect_operation_id="effect:a", order_ref="order:1"),
        )
    )

    assert PositionDriftCause.from_mapping(position_cause.to_mapping()).positions[0].symbol == "QQQ"
    assert (
        OrderOutcomeUnknownCause.from_mapping(order_cause.to_mapping()).identities[0].effect_operation_id
        == "effect:a"
    )


# ── raise_uncertainty / resolve_uncertainty ─────────────────────────────────


def test_raise_uncertainty_account_clerk_scope(repo: ClerkSqliteRepository) -> None:
    created = _raise(repo, strategy_instance_id=None)
    assert created is True
    uncertainty = repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code="ORDER_OUTCOME_UNKNOWN", strategy_instance_id=None
    )
    assert uncertainty is not None


def test_raise_uncertainty_bot_scope(repo: ClerkSqliteRepository) -> None:
    created = _raise(repo, strategy_instance_id=SID)
    assert created is True
    uncertainty = repo.active_uncertainty(scope="BOT", reason_code="ORDER_OUTCOME_UNKNOWN", strategy_instance_id=SID)
    assert uncertainty is not None


def test_raise_uncertainty_is_idempotent(repo: ClerkSqliteRepository) -> None:
    first = _raise(repo, strategy_instance_id=SID)
    before = len(repo.custody_transitions())
    second = _raise(repo, strategy_instance_id=SID)
    assert first is True
    assert second is False
    assert len(repo.custody_transitions()) == before


def test_evidence_backed_resolution_allows_a_fresh_raise_afterward(
    repo: ClerkSqliteRepository,
) -> None:
    _raise(repo, strategy_instance_id=None, reason_code="POSITION_DRIFT")
    uncertainty = repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code="POSITION_DRIFT", strategy_instance_id=None
    )
    assert uncertainty is not None

    resolve_reconciliation_uncertainty(repo, reason_code="POSITION_DRIFT", evidence_refs=("fresh_snapshot",))
    assert (
        repo.active_uncertainty(scope="ACCOUNT_CLERK", reason_code="POSITION_DRIFT", strategy_instance_id=None) is None
    )

    reraised = _raise(repo, strategy_instance_id=None, reason_code="POSITION_DRIFT")
    assert reraised is True


def test_raise_uncertainty_default_shape_fails_closed(repo: ClerkSqliteRepository) -> None:
    """#1380 acceptance: unrecognized reasons/facts default to ACCOUNT_CLERK
    and block new exposure — a caller that doesn't override severity/
    blocks_new_exposure/scope gets the fail-closed default for free."""
    _raise(repo, strategy_instance_id=SID, reason_code="SOME_NEW_UNCATALOGUED_SITUATION")
    uncertainty = repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code="SOME_NEW_UNCATALOGUED_SITUATION", strategy_instance_id=None
    )
    assert uncertainty is not None
    assert uncertainty["scope"] == "ACCOUNT_CLERK"
    assert uncertainty["blocks_new_exposure"] == 1
    assert uncertainty["allows_reduction"] == 0
    assert uncertainty["facts_schema_version"] == 1
    assert '"cause_facts"' in uncertainty["facts_json"]


# ── admit_new_exposure ───────────────────────────────────────────────────────


def test_admit_new_exposure_allows_when_nothing_active(repo: ClerkSqliteRepository) -> None:
    decision = admit_new_exposure(repo, strategy_instance_id=SID)
    assert decision.allowed is True


def test_admit_new_exposure_blocked_by_account_clerk_uncertainty_blocks_every_bot(
    repo: ClerkSqliteRepository,
) -> None:
    _raise(repo, strategy_instance_id=None, reason_code="ACCOUNT_WIDE_ISSUE")

    for sid in (SID, OTHER_SID):
        decision = admit_new_exposure(repo, strategy_instance_id=sid)
        assert decision.allowed is False
        assert decision.reason_code == "ACCOUNT_WIDE_ISSUE"


def test_admit_new_exposure_bot_scoped_uncertainty_blocks_only_that_bot(
    repo: ClerkSqliteRepository,
) -> None:
    """#1380 acceptance: BOT uncertainty blocks only the affected bot while
    account truth is fresh; unrelated bots keep trading."""
    _raise(repo, strategy_instance_id=SID, reason_code="ORDER_OUTCOME_UNKNOWN")

    blocked = admit_new_exposure(repo, strategy_instance_id=SID)
    assert blocked.allowed is False
    assert blocked.reason_code == "ORDER_OUTCOME_UNKNOWN"

    unaffected = admit_new_exposure(repo, strategy_instance_id=OTHER_SID)
    assert unaffected.allowed is True


def test_reduction_requires_a_recognized_cause_that_allows_it(
    repo: ClerkSqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repo, "attributed_positions_by_symbol", lambda: {"SPY": 5.0})
    _raise(
        repo,
        strategy_instance_id=None,
        reason_code="POSITION_DRIFT",
        cause_facts={"positions": [{"symbol": "SPY", "broker_qty": 5.0, "attributed_qty": 5.0}]},
    )
    allowed = decide_capability(
        repo,
        capability=Capability.REDUCE,
        strategy_instance_id=SID,
        reduction_intent=ReductionIntent(symbol="SPY", side="SELL", quantity=1.0),
    )
    assert allowed.allowed is True


def test_reduction_fails_closed_for_future_or_action_mismatched_drift_facts(
    repo: ClerkSqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repo, "attributed_positions_by_symbol", lambda: {"SPY": 5.0})
    _raise(
        repo,
        strategy_instance_id=None,
        reason_code="POSITION_DRIFT",
        cause_facts={
            "positions": [{"symbol": "SPY", "broker_qty": 5.0, "attributed_qty": 5.0}],
            "future_authorization": True,
        },
    )
    decision = decide_capability(
        repo,
        capability=Capability.REDUCE,
        strategy_instance_id=SID,
        reduction_intent=ReductionIntent(symbol="SPY", side="SELL", quantity=1.0),
    )
    assert decision.allowed is False


@pytest.mark.parametrize(
    ("side", "quantity"),
    [("BUY", 1.0), ("SELL", 6.0)],
)
def test_reduction_must_reduce_both_proven_quantities_without_crossing(
    repo: ClerkSqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    quantity: float,
) -> None:
    monkeypatch.setattr(repo, "attributed_positions_by_symbol", lambda: {"SPY": 5.0})
    _raise(
        repo,
        strategy_instance_id=None,
        reason_code="POSITION_DRIFT",
        cause_facts={"positions": [{"symbol": "SPY", "broker_qty": 5.0, "attributed_qty": 5.0}]},
    )
    decision = decide_capability(
        repo,
        capability=Capability.REDUCE,
        strategy_instance_id=SID,
        reduction_intent=ReductionIntent(symbol="SPY", side=side, quantity=quantity),
    )
    assert decision.allowed is False


def test_reduction_requires_fresh_drift_evidence(repo: ClerkSqliteRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repo, "attributed_positions_by_symbol", lambda: {"SPY": 5.0})
    _raise(
        repo,
        strategy_instance_id=None,
        reason_code="POSITION_DRIFT",
        cause_facts={"positions": [{"symbol": "SPY", "broker_qty": 5.0, "attributed_qty": 5.0}]},
    )
    uncertainty = repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code="POSITION_DRIFT", strategy_instance_id=None
    )
    assert uncertainty is not None
    monkeypatch.setattr(repo, "_clock", lambda: uncertainty["observed_at_ms"] + 30_001)
    decision = decide_capability(
        repo,
        capability=Capability.REDUCE,
        strategy_instance_id=SID,
        reduction_intent=ReductionIntent(symbol="SPY", side="SELL", quantity=1.0),
    )
    assert decision.allowed is False


def test_unknown_cause_blocks_reduction_account_wide(repo: ClerkSqliteRepository) -> None:
    _raise(repo, strategy_instance_id=SID, reason_code="NEW_CAUSE")
    decision = decide_capability(repo, capability=Capability.REDUCE, strategy_instance_id=OTHER_SID)
    assert decision.allowed is False


def test_admit_new_exposure_blocked_by_an_active_hold(repo: ClerkSqliteRepository) -> None:
    """Admission folds the #1378 hold mechanism behind the same surface —
    a caller must be blocked by either, with no separate check needed."""
    facts = AccountHoldRaisedFacts(reason_code="UNEXPLAINED_ORDER", evidence_refs=["bo-1"])
    repo.append_transition(
        TransitionInput(
            transition_kind="ACCOUNT_HOLD_RAISED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code="ACCOUNT_HOLD_RAISED",
            facts_json=facts.to_facts_json(),
        )
    )
    decision = admit_new_exposure(repo, strategy_instance_id=SID)
    assert decision.allowed is False
    assert decision.reason_code == "UNEXPLAINED_ORDER"


@pytest.mark.parametrize("capability", [Capability.CANCEL, Capability.RECONCILE])
def test_safety_capabilities_remain_allowed_under_active_hold(
    repo: ClerkSqliteRepository, capability: Capability
) -> None:
    facts = AccountHoldRaisedFacts(reason_code="UNEXPLAINED_ORDER", evidence_refs=["bo-1"])
    repo.append_transition(
        TransitionInput(
            transition_kind="ACCOUNT_HOLD_RAISED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code="ACCOUNT_HOLD_RAISED",
            facts_json=facts.to_facts_json(),
        )
    )

    decision = decide_capability(
        repo,
        capability=capability,
        strategy_instance_id=SID,
    )

    assert decision.allowed is True


# ── require_admission ────────────────────────────────────────────────────────


def test_require_admission_is_silent_when_allowed(repo: ClerkSqliteRepository) -> None:
    require_admission(repo, strategy_instance_id=SID)  # must not raise


def test_require_admission_raises_when_blocked(repo: ClerkSqliteRepository) -> None:
    _raise(repo, strategy_instance_id=SID, reason_code="ORDER_OUTCOME_UNKNOWN")
    with pytest.raises(AdmissionBlockedError) as exc_info:
        require_admission(repo, strategy_instance_id=SID)
    assert exc_info.value.decision.reason_code == "ORDER_OUTCOME_UNKNOWN"
