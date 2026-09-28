"""Custody-fenced fee attribution survives fresh observations and mirror replay."""

from __future__ import annotations

from decimal import Decimal

from app.broker.alpaca.clerk.sqlite.fee_evidence import (
    FEE_EVIDENCE_MAX_AGE_MS,
    custody_fee_attribution,
    record_fee_evidence,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerActivity
from tests.broker.alpaca.clerk.sqlite.conftest import (
    DAY_PNL_SID,
    NOON,
    YESTERDAY_NOON,
    _accept_day_pnl_enter,
    _append_day_pnl_slice,
    _clock_at,
)


def _activity(key: str, kind: str, at: int, amount: float = 0) -> BrokerActivity:
    return BrokerActivity(
        broker="alpaca",
        activity_id=key,
        activity_type=kind,
        category="non_trade_activity",
        symbol=None,
        side=None,
        quantity=None,
        price=None,
        net_amount=amount,
        occurred_at_ms=at,
        observed_at_ms=NOON,
    )


def _seed(repo) -> None:
    accepted = _accept_day_pnl_enter(repo, decision_id="fee-slice")
    _append_day_pnl_slice(
        repo,
        accepted,
        execution_id="fractional-fill",
        side="BUY",
        quantity=0.125,
        price=400,
        occurred_at_ms=YESTERDAY_NOON,
    )


def test_pending_fee_becomes_observed_once_and_old_observation_does_not_reappear(day_pnl_repo) -> None:
    repo = day_pnl_repo
    _seed(repo)
    older = _activity("older", "CSD", YESTERDAY_NOON - 86_400_000, 1000)
    assert record_fee_evidence(repo, [older], checked_at_ms=NOON)
    pending = custody_fee_attribution(repo._conn, now_ms=NOON)
    assert pending.known and pending.total_for(f"bot:{DAY_PNL_SID}") == Decimal("0.01")
    fee = _activity("fee", "FEE", YESTERDAY_NOON, -0.05)
    assert record_fee_evidence(repo, [fee, older], checked_at_ms=NOON)
    observed = custody_fee_attribution(repo._conn, now_ms=NOON)
    assert observed.known and observed.total_for(f"bot:{DAY_PNL_SID}") == Decimal("0.05")
    assert observed.unobserved_cash_claim(cash_seen_before_ms=NOON) == Decimal("0.05")
    assert not record_fee_evidence(
        repo, [fee.model_copy(update={"observed_at_ms": NOON + 100}), older], checked_at_ms=NOON + 100
    )
    assert observed.unobserved_cash_claim(cash_seen_before_ms=NOON + 1) == 0


def test_truncated_and_stale_reads_fail_closed_instead_of_zero(day_pnl_repo) -> None:
    repo = day_pnl_repo
    _seed(repo)
    record_fee_evidence(repo, [], checked_at_ms=NOON)
    missing = custody_fee_attribution(repo._conn, now_ms=NOON)
    assert not missing.known
    record_fee_evidence(repo, [_activity("older", "CSD", YESTERDAY_NOON - 86_400_000)], checked_at_ms=NOON)
    assert custody_fee_attribution(repo._conn, now_ms=NOON).known
    stale = custody_fee_attribution(repo._conn, now_ms=NOON + FEE_EVIDENCE_MAX_AGE_MS + 1)
    assert not stale.known and any("stale" in reason for reason in stale.unresolved)


def test_fee_projection_is_rebuilt_from_original_evidence(tmp_path) -> None:
    clock = _clock_at(NOON)
    repo = ClerkSqliteRepository.initialize(account_id="PA-fee", artifacts_root=tmp_path, clock=clock)
    record_fee_evidence(
        repo,
        [_activity("fee", "FEE", YESTERDAY_NOON, -0.05), _activity("old", "CSD", YESTERDAY_NOON - 86_400_000)],
        checked_at_ms=NOON,
    )
    expected = custody_fee_attribution(repo._conn, now_ms=NOON)
    original = repo.custody_transitions()
    path = repo.db_path
    repo.close()
    path.rename(path.with_suffix(".backup"))
    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(account_id="PA-fee", artifacts_root=tmp_path, clock=clock)
    try:
        assert custody_fee_attribution(rebuilt._conn, now_ms=NOON) == expected
        assert rebuilt.custody_transitions() == original
    finally:
        rebuilt.close()


def test_simulated_custody_refuses_real_fee_evidence(tmp_path) -> None:
    import pytest

    repo = ClerkSqliteRepository.initialize(account_id="shadow:live", artifacts_root=tmp_path, clock=_clock_at(NOON))
    try:
        with pytest.raises(ValueError, match="simulated custody"):
            record_fee_evidence(repo, [], checked_at_ms=NOON)
        assert custody_fee_attribution(repo._conn, now_ms=NOON).known
    finally:
        repo.close()


def test_risk_window_uses_same_fee_projection_without_prior_days(day_pnl_repo) -> None:
    from datetime import date

    from app.utils.session_anchors import et_midnight_ms

    repo = day_pnl_repo
    _seed(repo)
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
    prior = custody_fee_attribution(
        repo._conn, now_ms=NOON, from_ms=et_midnight_ms(date(2026, 9, 4)), to_ms=et_midnight_ms(date(2026, 9, 5))
    )
    today = custody_fee_attribution(repo._conn, now_ms=NOON, from_ms=et_midnight_ms(date(2026, 9, 8)), to_ms=NOON)
    assert prior.known and prior.total_for(f"bot:{DAY_PNL_SID}") == Decimal("0.01")
    assert today.known and not today.shares


def test_complete_short_provider_page_proves_a_young_account(day_pnl_repo) -> None:
    repo = day_pnl_repo
    _seed(repo)
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
    assert custody_fee_attribution(repo._conn, now_ms=NOON).known


def test_undated_fee_is_unresolved_instead_of_disappearing(day_pnl_repo) -> None:
    row = _activity("undated", "FEE", YESTERDAY_NOON, -0.05).model_copy(update={"occurred_at_ms": None})
    record_fee_evidence(day_pnl_repo, [row], checked_at_ms=NOON, history_complete=True)
    projection = custody_fee_attribution(day_pnl_repo._conn, now_ms=NOON)
    assert not projection.known and projection.unattributed == Decimal("0.05")


async def test_background_producer_uses_completion_proof_without_ui(day_pnl_repo) -> None:
    from app.broker.alpaca.clerk.sqlite.fee_evidence_sync import FeeEvidenceSync
    from app.broker.contract.models import BrokerActivityEvidence

    class Read:
        async def read_activity_evidence(self) -> BrokerActivityEvidence:
            return BrokerActivityEvidence(activities=[], history_complete=True)

    _seed(day_pnl_repo)
    assert await FeeEvidenceSync(repo=day_pnl_repo, read=Read()).tick()
    assert custody_fee_attribution(day_pnl_repo._conn, now_ms=NOON).known


def test_future_checked_time_is_not_fresh_risk_evidence(day_pnl_repo) -> None:
    record_fee_evidence(day_pnl_repo, [], checked_at_ms=NOON + 1, history_complete=True)
    result = custody_fee_attribution(day_pnl_repo._conn, now_ms=NOON)
    assert not result.known
    assert any("stale" in reason for reason in result.unresolved)
    # A fresh delivery after a clock correction must replace the bad freshness
    # stamp, even though its economic rows are unchanged.
    assert record_fee_evidence(day_pnl_repo, [], checked_at_ms=NOON, history_complete=True)
    assert custody_fee_attribution(day_pnl_repo._conn, now_ms=NOON).known


def test_corrected_fill_reprices_original_fee_day(day_pnl_repo) -> None:
    from app.broker.alpaca.clerk.sqlite.facts import ExecutionCorrectedFacts
    from tests.broker.alpaca.clerk.sqlite.test_folds_execution import _correction_transition

    repo = day_pnl_repo
    accepted = _accept_day_pnl_enter(repo, decision_id="corrected-fee")
    _append_day_pnl_slice(repo, accepted, execution_id="old", side="BUY", quantity=0.125, price=400, occurred_at_ms=YESTERDAY_NOON)
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
    assert custody_fee_attribution(repo._conn, now_ms=NOON).total_for(f"bot:{DAY_PNL_SID}") == Decimal("0.01")
    correction = ExecutionCorrectedFacts(execution_id="corrected", superseded_execution_ref="old", symbol="SPY", side="BUY", corrected_qty=4000, corrected_price=400, why="provider corrected quantity")
    assert repo.append_execution_correction_or_raise(
        correction=_correction_transition(repo, accepted=accepted, facts=correction, source_event_at_ms=NOON),
        build_uncertainty=lambda reason: (_ for _ in ()).throw(AssertionError(reason)),
    ) == "appended"
    revised = custody_fee_attribution(repo._conn, now_ms=NOON)
    assert revised.known and revised.total_for(f"bot:{DAY_PNL_SID}") == Decimal("0.02")
    from datetime import date

    from app.utils.session_anchors import et_midnight_ms

    today = custody_fee_attribution(repo._conn, now_ms=NOON, from_ms=et_midnight_ms(date(2026, 9, 8)), to_ms=NOON)
    assert today.known and not today.shares


def test_one_observed_order_fee_keeps_other_order_provision_through_custody(tmp_path) -> None:
    now = NOON + 86_400_000
    repo = ClerkSqliteRepository.initialize(account_id="PA-partial-fees", artifacts_root=tmp_path, clock=_clock_at(now))
    try:
        fills = [_activity(key, "FILL", NOON).model_copy(update={
            "native_order_id": f"order-{key}", "symbol": "SPY", "side": "sell",
            "quantity": 1000, "price": 100, "net_amount": None,
        }) for key in ("a", "b")]
        fee = _activity("fee-a", "FEE", NOON, -2).model_copy(update={"native_order_id": "order-a"})
        record_fee_evidence(repo, [*fills, fee], checked_at_ms=now, history_complete=True)
        observed = custody_fee_attribution(repo._conn, now_ms=now)
        assert observed.known
        assert observed.total_for("external:order-a") == Decimal("2.00")
        assert observed.total_for("external:order-b") == Decimal("2.25")
        assert observed.unobserved_cash_claim(cash_seen_before_ms=now) == Decimal("2.25")
        # A newer cash read cannot release this still-unmatched obligation.
        assert repo.account_budget(cash=1000, seen_before_ms=now).fee_claims == Decimal("2.25")
    finally:
        repo.close()
