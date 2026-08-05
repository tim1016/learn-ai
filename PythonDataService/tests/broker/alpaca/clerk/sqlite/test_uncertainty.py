"""Typed uncertainty envelope and admission tests (#1380, Part A).

Covers: BOT uncertainty blocks only the affected bot; ACCOUNT_CLERK blocks
every bot; unrecognized/default-shaped uncertainties fail closed; admission
is one function checking both uncertainties and the #1378 hold mechanism;
raise/resolve are idempotent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.facts import AccountHoldRaisedFacts
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    admit_new_exposure,
    raise_uncertainty,
    require_admission,
    resolve_uncertainty,
)

ACCOUNT_ID = "PA-TEST"
SID = "spy-bot"
OTHER_SID = "qqq-bot"


def _clock_seq():
    counter = {"t": 1_700_000_000_000}

    def clock() -> int:
        counter["t"] += 1
        return counter["t"]

    return clock


@pytest.fixture
def repo(tmp_path: Path):
    r = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    r.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    r.register_strategy_instance(strategy_instance_id=OTHER_SID, symbol="QQQ", config_hash="h2")
    yield r
    r.close()


def _raise(repo: ClerkSqliteRepository, *, strategy_instance_id: str | None, **overrides) -> bool:
    kwargs = {
        "reason_code": "TEST_REASON",
        "headline": "headline",
        "explanation": "explanation",
        "operator_impact": "operator impact",
        "next_step": "next step",
    }
    kwargs.update(overrides)
    return raise_uncertainty(repo, strategy_instance_id=strategy_instance_id, **kwargs)


# ── raise_uncertainty / resolve_uncertainty ─────────────────────────────────


def test_raise_uncertainty_account_clerk_scope(repo: ClerkSqliteRepository) -> None:
    created = _raise(repo, strategy_instance_id=None)
    assert created is True
    uncertainty = repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code="TEST_REASON", strategy_instance_id=None
    )
    assert uncertainty is not None


def test_raise_uncertainty_bot_scope(repo: ClerkSqliteRepository) -> None:
    created = _raise(repo, strategy_instance_id=SID)
    assert created is True
    uncertainty = repo.active_uncertainty(scope="BOT", reason_code="TEST_REASON", strategy_instance_id=SID)
    assert uncertainty is not None


def test_raise_uncertainty_is_idempotent(repo: ClerkSqliteRepository) -> None:
    first = _raise(repo, strategy_instance_id=SID)
    before = len(repo.custody_transitions())
    second = _raise(repo, strategy_instance_id=SID)
    assert first is True
    assert second is False
    assert len(repo.custody_transitions()) == before


def test_resolve_uncertainty_allows_a_fresh_raise_afterward(repo: ClerkSqliteRepository) -> None:
    _raise(repo, strategy_instance_id=SID)
    uncertainty = repo.active_uncertainty(scope="BOT", reason_code="TEST_REASON", strategy_instance_id=SID)
    assert uncertainty is not None

    resolve_uncertainty(repo, uncertainty_id=uncertainty["uncertainty_id"], resolution_note="fixed")
    assert (
        repo.active_uncertainty(scope="BOT", reason_code="TEST_REASON", strategy_instance_id=SID) is None
    )

    reraised = _raise(repo, strategy_instance_id=SID)
    assert reraised is True


def test_raise_uncertainty_default_shape_fails_closed(repo: ClerkSqliteRepository) -> None:
    """#1380 acceptance: unrecognized reasons/facts default to ACCOUNT_CLERK
    and block new exposure — a caller that doesn't override severity/
    blocks_new_exposure/scope gets the fail-closed default for free."""
    _raise(repo, strategy_instance_id=None, reason_code="SOME_NEW_UNCATALOGUED_SITUATION")
    uncertainty = repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code="SOME_NEW_UNCATALOGUED_SITUATION", strategy_instance_id=None
    )
    assert uncertainty is not None
    assert uncertainty["scope"] == "ACCOUNT_CLERK"
    assert uncertainty["blocks_new_exposure"] == 1


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
    _raise(repo, strategy_instance_id=SID, reason_code="SPY_SPECIFIC_ISSUE")

    blocked = admit_new_exposure(repo, strategy_instance_id=SID)
    assert blocked.allowed is False
    assert blocked.reason_code == "SPY_SPECIFIC_ISSUE"

    unaffected = admit_new_exposure(repo, strategy_instance_id=OTHER_SID)
    assert unaffected.allowed is True


def test_admit_new_exposure_ignores_an_uncertainty_that_does_not_block_new_exposure(
    repo: ClerkSqliteRepository,
) -> None:
    """Some uncertainties are informational only — reduction/cancellation
    always stays available (issue framing), and this one also permits new
    exposure to continue."""
    _raise(repo, strategy_instance_id=SID, reason_code="INFO_ONLY", blocks_new_exposure=False)
    decision = admit_new_exposure(repo, strategy_instance_id=SID)
    assert decision.allowed is True


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


# ── require_admission ────────────────────────────────────────────────────────


def test_require_admission_is_silent_when_allowed(repo: ClerkSqliteRepository) -> None:
    require_admission(repo, strategy_instance_id=SID)  # must not raise


def test_require_admission_raises_when_blocked(repo: ClerkSqliteRepository) -> None:
    _raise(repo, strategy_instance_id=SID, reason_code="SPY_SPECIFIC_ISSUE")
    with pytest.raises(AdmissionBlockedError) as exc_info:
        require_admission(repo, strategy_instance_id=SID)
    assert exc_info.value.decision.reason_code == "SPY_SPECIFIC_ISSUE"
