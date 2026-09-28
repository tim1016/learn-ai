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
    return BrokerActivity(broker="alpaca", activity_id=key, activity_type=kind,
        category="non_trade_activity", symbol=None, side=None, quantity=None,
        price=None, net_amount=amount, occurred_at_ms=at, observed_at_ms=NOON)


def _seed(repo) -> None:
    accepted = _accept_day_pnl_enter(repo, decision_id="fee-slice")
    _append_day_pnl_slice(repo, accepted, execution_id="fractional-fill", side="BUY",
        quantity=0.125, price=400, occurred_at_ms=YESTERDAY_NOON)


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
    assert not record_fee_evidence(repo, [fee.model_copy(update={"observed_at_ms": NOON + 100}), older], checked_at_ms=NOON + 100)
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
    record_fee_evidence(repo, [_activity("fee", "FEE", YESTERDAY_NOON, -0.05), _activity("old", "CSD", YESTERDAY_NOON - 86_400_000)], checked_at_ms=NOON)
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
