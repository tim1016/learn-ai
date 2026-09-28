"""Custody-fenced fee attribution survives fresh observations and mirror replay."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.budget_projection import BudgetUnavailable, _external_cash_claim
from app.broker.alpaca.clerk.sqlite.fee_evidence import (
    FEE_EVIDENCE_KIND,
    FEE_EVIDENCE_MAX_AGE_MS,
    FeeEvidenceFacts,
    custody_fee_attribution,
    record_fee_evidence,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerActivity
from app.services.session_authority import et_minute_of_day_ms
from tests.broker.alpaca.clerk.sqlite.conftest import (
    DAY_PNL_SID,
    NOON,
    YESTERDAY_NOON,
    YESTERDAY_OPEN,
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
    pending = repo.fee_attribution(now_ms=NOON)
    assert pending.known and pending.total_for(f"bot:{DAY_PNL_SID}") == Decimal("0.01")
    fee = _activity("fee", "FEE", YESTERDAY_NOON, -0.05)
    assert record_fee_evidence(repo, [fee, older], checked_at_ms=NOON)
    observed = repo.fee_attribution(now_ms=NOON)
    assert observed.known and observed.total_for(f"bot:{DAY_PNL_SID}") == Decimal("0.05")
    assert observed.unobserved_cash_claim(cash_seen_before_ms=NOON) == Decimal("0.05")
    assert not record_fee_evidence(
        repo, [fee.model_copy(update={"observed_at_ms": NOON + 100}), older], checked_at_ms=NOON + 100
    )
    assert observed.unobserved_cash_claim(cash_seen_before_ms=NOON + 1) == 0


def _fee_records(repo: ClerkSqliteRepository) -> list[FeeEvidenceFacts]:
    return [
        FeeEvidenceFacts.model_validate_json(row["facts_json"])
        for row in repo.custody_transitions()
        if row["transition_kind"] == FEE_EVIDENCE_KIND
    ]


def test_unchanged_polls_append_no_activity_payload_and_stay_fresh(day_pnl_repo) -> None:
    """The producer polls every 15 s; an unchanged account appends nothing (#2550).

    Liveness is proven by the latest read, the way the account observation
    proves cash freshness, never by re-copying the activity window into the
    hash chain on every other tick.
    """
    repo = day_pnl_repo
    _seed(repo)
    window = [_activity(f"row-{index}", "CSD", YESTERDAY_NOON - 86_400_000 + index) for index in range(300)]
    ticks = [NOON + index * 15_000 for index in range(10)]
    for at in ticks:
        record_fee_evidence(repo, [row.model_copy(update={"observed_at_ms": at}) for row in window], checked_at_ms=at)
    records = _fee_records(repo)
    assert len(records) == 1 and sum(len(record.activities) for record in records) == 300
    fresh = repo.fee_attribution(now_ms=ticks[-1] + FEE_EVIDENCE_MAX_AGE_MS)
    assert fresh.known and fresh.total_for(f"bot:{DAY_PNL_SID}") == Decimal("0.01")
    assert not repo.fee_attribution(now_ms=ticks[-1] + FEE_EVIDENCE_MAX_AGE_MS + 1).known

    fee = _activity("fee", "FEE", YESTERDAY_NOON, -0.05)
    later = ticks[-1] + 15_000
    assert record_fee_evidence(repo, [fee, *window], checked_at_ms=later)
    assert [row.activity_id for row in _fee_records(repo)[-1].activities] == ["fee"]
    observed = repo.fee_attribution(now_ms=later)
    assert observed.known and observed.total_for(f"bot:{DAY_PNL_SID}") == Decimal("0.05")

    conflicting = fee.model_copy(update={"net_amount": -0.06, "observed_at_ms": later + 15_000})
    assert record_fee_evidence(repo, [conflicting, *window], checked_at_ms=later + 15_000)
    assert not repo.fee_attribution(now_ms=later + 15_000).known
    # The same conflicting redelivery adds nothing and still fails closed.
    assert not record_fee_evidence(repo, [conflicting, *window], checked_at_ms=later + 30_000)
    assert not repo.fee_attribution(now_ms=later + 30_000).known
    assert len(_fee_records(repo)) == 3


def test_delta_records_project_the_same_attribution_as_one_full_union(tmp_path) -> None:
    """Unioning per-read deltas equals unioning every full window in order."""
    first = _activity("older", "CSD", YESTERDAY_NOON - 86_400_000, 1000)
    fee = _activity("fee", "FEE", YESTERDAY_NOON, -0.05)
    windows = [[first], [fee.model_copy(update={"observed_at_ms": NOON + 1}), first], [fee, first]]
    projections = []
    for name, reads in (("PA-delta", windows), ("PA-union", [[row for window in windows for row in window]])):
        # Custody's chain begins on the fee day; older history is outside it.
        repo = ClerkSqliteRepository.initialize(account_id=name, artifacts_root=tmp_path, clock=_clock_at(YESTERDAY_NOON))
        try:
            for read in reads:
                record_fee_evidence(repo, read, checked_at_ms=NOON)
            projections.append(custody_fee_attribution(repo._conn, now_ms=NOON, evidence_checked_at_ms=NOON))
        finally:
            repo.close()
    assert projections[0] == projections[1]
    assert [charge.observed_at_ms for charge in projections[0].unattributed_charges] == [NOON + 1]


def test_truncated_and_stale_reads_fail_closed_instead_of_zero(day_pnl_repo) -> None:
    repo = day_pnl_repo
    _seed(repo)
    record_fee_evidence(repo, [], checked_at_ms=NOON)
    missing = repo.fee_attribution(now_ms=NOON)
    assert not missing.known
    record_fee_evidence(repo, [_activity("older", "CSD", YESTERDAY_NOON - 86_400_000)], checked_at_ms=NOON)
    assert repo.fee_attribution(now_ms=NOON).known
    stale = repo.fee_attribution(now_ms=NOON + FEE_EVIDENCE_MAX_AGE_MS + 1)
    assert not stale.known and any("stale" in reason for reason in stale.unresolved)
    # No control performs "Refresh account evidence"; the reason names the cause only.
    assert "Account fee evidence is missing or stale." in stale.unresolved
    assert not any("Refresh" in reason for reason in stale.unresolved)


def test_fee_projection_is_rebuilt_from_original_evidence(tmp_path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="PA-fee", artifacts_root=tmp_path, clock=_clock_at(YESTERDAY_NOON))
    record_fee_evidence(
        repo,
        [_activity("fee", "FEE", YESTERDAY_NOON, -0.05), _activity("old", "CSD", YESTERDAY_NOON - 86_400_000)],
        checked_at_ms=NOON,
    )
    expected = custody_fee_attribution(repo._conn, now_ms=NOON, evidence_checked_at_ms=NOON)
    assert expected.unattributed == Decimal("0.05")
    original = repo.custody_transitions()
    path = repo.db_path
    repo.close()
    path.rename(path.with_suffix(".backup"))
    # A rebuild re-stamps control_meta.created_at_ms; custody's history floor
    # comes from the replayed chain, so the rebuilt day keeps the fee day.
    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(account_id="PA-fee", artifacts_root=tmp_path, clock=_clock_at(NOON))
    try:
        assert custody_fee_attribution(rebuilt._conn, now_ms=NOON, evidence_checked_at_ms=NOON) == expected
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
        repo._conn, now_ms=NOON, evidence_checked_at_ms=NOON, from_ms=et_midnight_ms(date(2026, 9, 4)), to_ms=et_midnight_ms(date(2026, 9, 5))
    )
    today = custody_fee_attribution(repo._conn, now_ms=NOON, evidence_checked_at_ms=NOON, from_ms=et_midnight_ms(date(2026, 9, 8)), to_ms=NOON)
    assert prior.known and prior.total_for(f"bot:{DAY_PNL_SID}") == Decimal("0.01")
    assert today.known and not today.shares


def test_complete_short_provider_page_proves_a_young_account(day_pnl_repo) -> None:
    repo = day_pnl_repo
    _seed(repo)
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
    assert repo.fee_attribution(now_ms=NOON).known


def test_undated_fee_is_unresolved_instead_of_disappearing(day_pnl_repo) -> None:
    row = _activity("undated", "FEE", YESTERDAY_NOON, -0.05).model_copy(update={"occurred_at_ms": None})
    record_fee_evidence(day_pnl_repo, [row], checked_at_ms=NOON, history_complete=True)
    projection = day_pnl_repo.fee_attribution(now_ms=NOON)
    assert not projection.known and projection.unattributed == Decimal("0.05")


async def test_background_producer_uses_completion_proof_without_ui(day_pnl_repo) -> None:
    from app.broker.alpaca.clerk.sqlite.fee_evidence_sync import FeeEvidenceSync
    from app.broker.contract.models import BrokerActivityEvidence

    class Read:
        async def read_activity_evidence(self) -> BrokerActivityEvidence:
            return BrokerActivityEvidence(activities=[], history_complete=True)

    _seed(day_pnl_repo)
    assert await FeeEvidenceSync(repo=day_pnl_repo, read=Read()).tick()
    assert day_pnl_repo.fee_attribution(now_ms=NOON).known


class _PagedHistory:
    """Newest-first provider history read like the adapter: 3 pages of 100."""

    def __init__(self, rows: list[BrokerActivity]) -> None:
        self.rows = rows
        self.tokens: list[str | None] = []

    async def read_activity_evidence(self, *, page_token: str | None = None):
        from app.broker.contract.models import BrokerActivityEvidence

        self.tokens.append(page_token)
        start = 0 if page_token is None else [row.activity_id for row in self.rows].index(page_token) + 1
        read: list[BrokerActivity] = []
        for _ in range(3):
            page = self.rows[start:start + 100]
            read.extend(page)
            if len(page) < 100:
                return BrokerActivityEvidence(activities=read, history_complete=True)
            start += 100
        return BrokerActivityEvidence(activities=read, history_complete=False, next_page_token=read[-1].activity_id)


async def test_busy_account_backfills_to_its_oldest_fill_day_instead_of_bricking(day_pnl_repo) -> None:
    """More than 300 newer activities once made an old fill day uncoverable (#2550).

    Every head read is the newest 300 rows, so the yesterday fill day was never
    covered and every deploy/ENTER was refused forever. The producer now walks
    the rest of history from the cursor custody retained, one bounded read a
    tick, and stays fail-closed until that walk passes the fill day.
    """
    from app.broker.alpaca.clerk.sqlite.fee_evidence_sync import FeeEvidenceSync

    repo = day_pnl_repo
    _seed(repo)
    today = [_activity(f"row-{index}", "CSD", NOON - (index + 1) * 1_000) for index in range(900)]
    older = [_activity(f"row-{900 + index}", "CSD", YESTERDAY_NOON - 86_400_000 - index) for index in range(100)]
    history = _PagedHistory([*today, *older])
    known = []
    for sync in (FeeEvidenceSync(repo=repo, read=history),) * 2 + (FeeEvidenceSync(repo=repo, read=history),):
        await sync.tick()
        known.append(repo.fee_attribution(now_ms=NOON).known)
    assert known == [False, False, True]
    # A fresh producer (restart) resumes from the retained cursor.
    assert history.tokens == [None, "row-299", None, "row-599", None, "row-899"]
    await FeeEvidenceSync(repo=repo, read=history).tick()
    assert history.tokens[-1:] == [None] and repo.fee_attribution(now_ms=NOON).known


async def test_backfill_keeps_an_uncovered_fill_day_fail_closed(day_pnl_repo) -> None:
    """Relevant days stay refused until a linked read reaches them."""
    repo = day_pnl_repo
    _seed(repo)
    head = [_activity(f"row-{index}", "CSD", NOON - (index + 1) * 1_000) for index in range(300)]
    assert record_fee_evidence(repo, head, checked_at_ms=NOON, next_page_token="row-299")
    assert not repo.fee_attribution(now_ms=NOON).known
    # A read that did not resume at the retained cursor proves no contiguity.
    stray = _activity("stray", "CSD", YESTERDAY_NOON - 86_400_000)
    record_fee_evidence(repo, [stray], checked_at_ms=NOON, history_complete=True, page_token="not-the-cursor")
    assert not repo.fee_attribution(now_ms=NOON).known
    # The linked continuation covers the day; later heads never uncover it.
    assert record_fee_evidence(repo, [stray], checked_at_ms=NOON, page_token="row-299", next_page_token="stray")
    assert repo.fee_attribution(now_ms=NOON).known
    record_fee_evidence(repo, head, checked_at_ms=NOON + 15_000, next_page_token="row-299")
    assert repo.fee_attribution(now_ms=NOON + 15_000).known


def _outside_fill(key: str, at: int, *, side: str = "buy", order: str = "manual-order-1") -> BrokerActivity:
    return BrokerActivity(
        broker="alpaca", activity_id=key, native_order_id=order, activity_type="FILL",
        category="trade_activity", symbol="SPY", side=side, quantity=1.0, price=100.0,
        net_amount=None, occurred_at_ms=at, observed_at_ms=NOON,
    )


# Outside fills older than custody: a BUY before the pinned fee rates and a
# short sale the fee model cannot price. Walking history to the provider's
# exhaustion made either one refuse every deploy and ENTER forever (#2550).
_ANCIENT_OUTSIDE_FILLS = pytest.mark.parametrize(
    "ancient",
    [
        _outside_fill("ancient-buy", et_minute_of_day_ms(date(2026, 8, 20), 12 * 60)),
        _outside_fill("ancient-short", et_minute_of_day_ms(date(2026, 9, 2), 12 * 60), side="sell_short"),
    ],
    ids=["unpinned-rate-buy", "sell-short"],
)


@_ANCIENT_OUTSIDE_FILLS
async def test_head_read_at_custody_floor_never_walks_into_older_history(day_pnl_repo, ancient) -> None:
    """Custody's floor bounds the walk; history before it is never read (#2550)."""
    from app.broker.alpaca.clerk.sqlite.fee_evidence_sync import FeeEvidenceSync

    repo = day_pnl_repo
    accepted = _accept_day_pnl_enter(repo, decision_id="today")
    _append_day_pnl_slice(repo, accepted, execution_id="today-fill", side="BUY", quantity=1, price=400, occurred_at_ms=NOON - 1_000)
    recent = [_activity(f"row-{index}", "CSD", NOON - (index + 1) * 300_000) for index in range(600)]
    older = [_activity(f"old-{index}", "CSD", ancient.occurred_at_ms - (index + 1) * 1_000) for index in range(10)]
    history = _PagedHistory([*recent, ancient, *older])
    known = []
    for _ in range(3):
        await FeeEvidenceSync(repo=repo, read=history).tick()
        known.append(repo.fee_attribution(now_ms=NOON).known)
    assert known == [True, True, True]
    # The head already reached custody's only day: no continuation is due.
    assert history.tokens == [None, None, None]


@_ANCIENT_OUTSIDE_FILLS
async def test_walk_stops_at_custody_floor_and_never_attributes_older_rows(day_pnl_repo, ancient) -> None:
    """The read that crosses the floor may carry older rows; they never count (#2550)."""
    from app.broker.alpaca.clerk.sqlite.fee_evidence_sync import FeeEvidenceSync

    repo = day_pnl_repo
    _seed(repo)  # custody's oldest fill day, before its genesis today, is the floor
    today = [_activity(f"row-{index}", "CSD", NOON - (index + 1) * 1_000) for index in range(900)]
    before_floor = [_activity(f"row-{900 + index}", "CSD", YESTERDAY_NOON - 86_400_000 - index) for index in range(150)]
    beyond = [_activity(f"row-{1051 + index}", "CSD", ancient.occurred_at_ms - (index + 1) * 1_000) for index in range(500)]
    history = _PagedHistory([*today, *before_floor, ancient, *beyond])
    known = []
    for _ in range(4):
        await FeeEvidenceSync(repo=repo, read=history).tick()
        known.append(repo.fee_attribution(now_ms=NOON).known)
    assert known == [False, False, True, True]
    assert history.tokens == [None, "row-299", None, "row-599", None, "row-899", None]
    assert ancient.activity_id in {row.activity_id for record in _fee_records(repo) for row in record.activities}
    assert not repo.fee_attribution(now_ms=NOON).external_fills


def test_outside_fills_attribute_from_the_custody_floor_day_on(day_pnl_repo) -> None:
    """Only days before the floor leave attribution; the floor day still claims (#2550)."""
    repo = day_pnl_repo
    _seed(repo)
    on_floor = _outside_fill("floor-day-buy", YESTERDAY_OPEN, order="floor-day-order")
    before_floor = _outside_fill("day-before-buy", YESTERDAY_NOON - 86_400_000, order="day-before-order")
    record_fee_evidence(repo, [on_floor, before_floor], checked_at_ms=NOON, history_complete=True)
    result = repo.fee_attribution(now_ms=NOON)
    assert result.known
    assert [fill.fill_id for fill in result.external_fills] == ["floor-day-buy"]
    assert result.total_for("external:floor-day-order") == Decimal("0.01")
    assert result.unobserved_cash_claim(cash_seen_before_ms=NOON) == Decimal("0.01")


def test_outside_short_sale_on_a_custody_day_still_refuses(day_pnl_repo) -> None:
    repo = day_pnl_repo
    _seed(repo)
    short = _outside_fill("floor-day-short", YESTERDAY_OPEN, side="sell_short")
    record_fee_evidence(repo, [short], checked_at_ms=NOON, history_complete=True)
    result = repo.fee_attribution(now_ms=NOON)
    assert not result.known
    assert "Account fill coverage is incomplete. Reconcile account executions before deploying." in result.unresolved


def _observe_tracked_gtc(repo: ClerkSqliteRepository) -> None:
    """An outside GTC order custody tracks: 2 of 5 shares filled, then canceled."""
    from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
    from app.broker.contract.models import BrokerOrder

    observe_external_order(repo, order=BrokerOrder(
        broker="alpaca", order_id="gtc-order", client_order_id="console-gtc", symbol="SPY",
        asset_class="us_equity", side="buy", order_type="limit", time_in_force="gtc", quantity=5,
        filled_quantity=2, limit_price=100, stop_price=None, filled_avg_price=100, status="canceled",
        submitted_at_ms=None, created_at_ms=None, updated_at_ms=None, filled_at_ms=None,
        canceled_at_ms=None, expired_at_ms=None, observed_at_ms=NOON,
    ))


async def test_walk_reaches_every_execution_of_a_tracked_external_order(day_pnl_repo) -> None:
    """The walk continues until a tracked external order is explained (#2550).

    Its executions predate custody, but the budget needs all of them. One
    witnessed fill is not enough: the walk continues until the order's
    filled quantity is explained, then stops short of exhaustion.
    """
    from app.broker.alpaca.clerk.sqlite.fee_evidence_sync import FeeEvidenceSync

    repo = day_pnl_repo
    _observe_tracked_gtc(repo)
    newer = _outside_fill("gtc-newer", YESTERDAY_NOON, order="gtc-order")
    older = _outside_fill("gtc-older", et_minute_of_day_ms(date(2026, 9, 2), 12 * 60), order="gtc-order")
    # The head reaches past custody's genesis day, so only the order drives the walk.
    head = [_activity(f"row-{index}", "CSD", NOON - (index + 1) * 300_000) for index in range(300)]
    between = [_activity(f"row-{301 + index}", "CSD", YESTERDAY_NOON - 86_400_000 - index) for index in range(599)]
    beyond = [_activity(f"row-{901 + index}", "CSD", older.occurred_at_ms - 86_400_000 - index) for index in range(600)]
    history = _PagedHistory([*head, newer, *between, older, *beyond])
    for _ in range(4):
        await FeeEvidenceSync(repo=repo, read=history).tick()
    assert history.tokens == [None, "row-299", None, "row-599", None, "row-899", None]
    result = repo.fee_attribution(now_ms=NOON)
    assert result.known
    assert sorted(fill.fill_id for fill in result.external_fills) == ["gtc-newer", "gtc-older"]


# The tracked GTC works across custody's genesis (09-08). Its earlier execution
# once widened the attribution floor to its own day, pricing it before the
# pinned fee rates or re-admitting an unrelated outside short sale custody
# never owned; either refused every deploy and ENTER forever (#2550).
@pytest.mark.parametrize(
    ("earlier_day", "unrelated"),
    [
        (date(2026, 8, 20), []),
        (date(2026, 9, 2), [_outside_fill(
            "unrelated-short", et_minute_of_day_ms(date(2026, 9, 3), 12 * 60), side="sell_short", order="unrelated-order"
        )]),
    ],
    ids=["unpinned-rate-execution", "unrelated-short-after-it"],
)
def test_tracked_order_execution_before_custody_reaches_only_the_cash_claim(
    day_pnl_repo, earlier_day, unrelated
) -> None:
    repo = day_pnl_repo
    _observe_tracked_gtc(repo)
    later = _outside_fill("gtc-later", NOON - 1_000, order="gtc-order")
    earlier = _outside_fill("gtc-earlier", et_minute_of_day_ms(earlier_day, 12 * 60), order="gtc-order")
    record_fee_evidence(repo, [later, *unrelated, earlier], checked_at_ms=NOON, history_complete=True)
    result = repo.fee_attribution(now_ms=NOON)
    assert result.known, result.unresolved
    assert sorted(fill.fill_id for fill in result.external_fills) == ["gtc-earlier", "gtc-later"]
    # Only the execution on custody's day is priced.
    assert result.total_for("external:gtc-order") == Decimal("0.01")
    # Both executions reach the claim's exact filled-quantity check (2 shares).
    assert _external_cash_claim(repo._conn, result, seen_before_ms=NOON) == Decimal(200)
    assert _external_cash_claim(repo._conn, result, seen_before_ms=NOON + 1) == 0


async def test_guard_refuses_to_answer_a_continuation_with_the_newest_rows() -> None:
    from app.broker.alpaca.clerk.sqlite.broker_port_guard import guard_broker_read_port
    from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
    from app.broker.contract.errors import BrokerEvidenceUnavailable

    class NewestOnly:
        async def list_activities(self, **_kwargs: object) -> list[BrokerActivity]:
            return []

    guarded = guard_broker_read_port(NewestOnly(), intake=ReentrantAsyncLock())  # type: ignore[arg-type]
    assert not (await guarded.read_activity_evidence()).history_complete
    with pytest.raises(BrokerEvidenceUnavailable):
        await guarded.read_activity_evidence(page_token="older")


async def test_guard_passes_a_windowed_evidence_read_through_unchanged() -> None:
    from app.broker.alpaca.clerk.sqlite.broker_port_guard import guard_broker_read_port
    from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
    from app.broker.contract.models import BrokerActivityEvidence

    class Paged:
        def __init__(self) -> None:
            self.calls: list[tuple[str | None, int | None]] = []

        async def read_activity_evidence(self, *, page_token: str | None = None, after_ms: int | None = None):
            self.calls.append((page_token, after_ms))
            return BrokerActivityEvidence(activities=[], history_complete=True)

    inner = Paged()
    guarded = guard_broker_read_port(inner, intake=ReentrantAsyncLock())  # type: ignore[arg-type]
    await guarded.read_activity_evidence(page_token="older", after_ms=NOON)
    assert inner.calls == [("older", NOON)]


def test_future_checked_time_is_not_fresh_risk_evidence(day_pnl_repo) -> None:
    record_fee_evidence(day_pnl_repo, [], checked_at_ms=NOON + 1, history_complete=True)
    result = day_pnl_repo.fee_attribution(now_ms=NOON)
    assert not result.known
    assert any("stale" in reason for reason in result.unresolved)
    # A fresh delivery after a clock correction must replace the bad freshness
    # stamp, even though its economic rows are unchanged (and append nothing).
    assert not record_fee_evidence(day_pnl_repo, [], checked_at_ms=NOON, history_complete=True)
    assert day_pnl_repo.fee_attribution(now_ms=NOON).known


def test_corrected_fill_reprices_original_fee_day(day_pnl_repo) -> None:
    from app.broker.alpaca.clerk.sqlite.facts import ExecutionCorrectedFacts
    from tests.broker.alpaca.clerk.sqlite.test_folds_execution import _correction_transition

    repo = day_pnl_repo
    accepted = _accept_day_pnl_enter(repo, decision_id="corrected-fee")
    _append_day_pnl_slice(repo, accepted, execution_id="old", side="BUY", quantity=0.125, price=400, occurred_at_ms=YESTERDAY_NOON)
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
    assert repo.fee_attribution(now_ms=NOON).total_for(f"bot:{DAY_PNL_SID}") == Decimal("0.01")
    correction = ExecutionCorrectedFacts(execution_id="corrected", superseded_execution_ref="old", symbol="SPY", side="BUY", corrected_qty=4000, corrected_price=400, why="provider corrected quantity")
    assert repo.append_execution_correction_or_raise(
        correction=_correction_transition(repo, accepted=accepted, facts=correction, source_event_at_ms=NOON),
        build_uncertainty=lambda reason: (_ for _ in ()).throw(AssertionError(reason)),
    ) == "appended"
    revised = repo.fee_attribution(now_ms=NOON)
    assert revised.known and revised.total_for(f"bot:{DAY_PNL_SID}") == Decimal("0.02")
    from datetime import date

    from app.utils.session_anchors import et_midnight_ms

    today = custody_fee_attribution(repo._conn, now_ms=NOON, evidence_checked_at_ms=NOON, from_ms=et_midnight_ms(date(2026, 9, 8)), to_ms=NOON)
    assert today.known and not today.shares


def test_one_observed_order_fee_keeps_other_order_provision_through_custody(tmp_path) -> None:
    now = NOON + 86_400_000
    # Custody's chain begins on the fills' day; older history is outside it.
    clock = _clock_at(NOON)
    repo = ClerkSqliteRepository.initialize(account_id="PA-partial-fees", artifacts_root=tmp_path, clock=clock)
    try:
        fills = [_activity(key, "FILL", NOON).model_copy(update={
            "native_order_id": f"order-{key}", "symbol": "SPY", "side": "sell",
            "quantity": 1000, "price": 100, "net_amount": None,
        }) for key in ("a", "b")]
        fee = _activity("fee-a", "FEE", NOON, -2).model_copy(update={"native_order_id": "order-a"})
        record_fee_evidence(repo, [*fills, fee], checked_at_ms=now, history_complete=True)
        clock.advance(now - NOON)
        observed = repo.fee_attribution(now_ms=now)
        assert observed.known
        assert observed.total_for("external:order-a") == Decimal("2.00")
        assert observed.total_for("external:order-b") == Decimal("2.25")
        assert observed.unobserved_cash_claim(cash_seen_before_ms=now) == Decimal("2.25")
        # A newer cash read cannot release this still-unmatched obligation.
        assert repo.account_budget(cash=1000, seen_before_ms=now).fee_claims == Decimal("2.25")
    finally:
        repo.close()


def test_unattributed_fee_stops_claiming_once_cash_observation_recognizes_it(tmp_path) -> None:
    """An account-unattributed fee counts once against availability (PRD #2540).

    The fee's order has no proven custody owner, so the charge stays at the
    account level. A cash observation taken after the fee evidence was
    observed already carries the debit; claiming it past that point counted
    the same dollar twice and understated availability forever.
    """
    now = NOON
    # Custody's chain begins on the fee day; older history is outside it.
    clock = _clock_at(YESTERDAY_NOON)
    repo = ClerkSqliteRepository.initialize(account_id="PA-unattributed-fee", artifacts_root=tmp_path, clock=clock)
    try:
        fee = _activity("fee-x", "FEE", YESTERDAY_NOON, -0.05).model_copy(update={"native_order_id": "order-x"})
        assert record_fee_evidence(repo, [fee], checked_at_ms=now, history_complete=True)
        clock.advance(now - YESTERDAY_NOON)
        observed = repo.fee_attribution(now_ms=now)
        assert not observed.known and observed.unattributed == Decimal("0.05")
        assert observed.unobserved_cash_claim(cash_seen_before_ms=now) == Decimal("0.05")
        assert observed.unobserved_cash_claim(cash_seen_before_ms=now + 1) == 0
        # Unresolved fee evidence refuses budget projection outright; once the
        # evidence resolves, the claim the projection would read is the gated
        # one above, not the old forever-claimed account total.
        with pytest.raises(BudgetUnavailable):
            repo.account_budget(cash=1000, seen_before_ms=now)
    finally:
        repo.close()


@pytest.mark.parametrize("account_id", ["sim:prior-close", "shadow:prior-close"])
def test_simulated_fee_cutoff_uses_inclusive_economic_time(tmp_path: Path, account_id: str) -> None:
    from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
    from app.broker.alpaca.clerk.sqlite.enter import accept_enter
    from app.broker.alpaca.clerk.sqlite.facts import ExecutionCorrectedFacts
    from app.broker.contract.models import BrokerOrderLeg
    from app.lean_sidecar.trading_calendar import previous_completed_session_close_ms
    from tests.broker.alpaca.clerk.sqlite.conftest import DAY_PNL_RUN_ID
    from tests.broker.alpaca.clerk.sqlite.test_folds_execution import _correction_transition

    close_ms = previous_completed_session_close_ms(NOON)
    repo = ClerkSqliteRepository.initialize(account_id=account_id, artifacts_root=tmp_path, clock=_clock_at(NOON))
    try:
        repo.register_strategy_instance(strategy_instance_id=DAY_PNL_SID, symbol="SPY", config_hash="close-fees")
        submit_start_run(repo, account_id=account_id, strategy_instance_id=DAY_PNL_SID,
                         lifecycle_run_id=DAY_PNL_RUN_ID, clock=repo.clock)
        accepted = accept_enter(repo, account_id=account_id, strategy_instance_id=DAY_PNL_SID,
                                decision_id="cutoff", lifecycle_run_id=DAY_PNL_RUN_ID,
                                leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=12_000))
        for key, quantity, at in (("before", 1, close_ms - 1), ("at-close", 4000, close_ms),
                                  ("after", 4000, close_ms + 1)):
            _append_day_pnl_slice(repo, accepted, execution_id=key, side="BUY", quantity=quantity,
                                 price=100, occurred_at_ms=at)
        transitions = repo.custody_transitions()
        historical = custody_fee_attribution(repo._conn, now_ms=NOON, simulated_fill_cutoff_ms=close_ms)
        current = custody_fee_attribution(repo._conn, now_ms=NOON)
        # CAT: ceil((1 + 4000) * .000003) = .02 at close, .03 after.
        # All facts were recorded today, so record time cannot select the fills.
        assert historical.known and current.known
        assert historical.total_for(f"bot:{DAY_PNL_SID}") == Decimal("0.02")
        assert current.total_for(f"bot:{DAY_PNL_SID}") == Decimal("0.03")
        assert {share.state for share in historical.shares} == {"modelled_settled"}
        assert repo.custody_transitions() == transitions
        correction = ExecutionCorrectedFacts(execution_id="corrected-before", superseded_execution_ref="before",
                                             symbol="SPY", side="BUY", corrected_qty=4000, corrected_price=100,
                                             why="correct original fill quantity")
        assert repo.append_execution_correction_or_raise(
            correction=_correction_transition(repo, accepted=accepted, facts=correction, source_event_at_ms=NOON),
            build_uncertainty=lambda reason: (_ for _ in ()).throw(AssertionError(reason)),
        ) == "appended"
        # A later correction inherits its root execution time, repricing the
        # selected population through the same model instead of moving today.
        corrected = custody_fee_attribution(repo._conn, now_ms=NOON, simulated_fill_cutoff_ms=close_ms)
        assert corrected.known and corrected.total_for(f"bot:{DAY_PNL_SID}") == Decimal("0.03")
        assert custody_fee_attribution(repo._conn, now_ms=NOON).total_for(f"bot:{DAY_PNL_SID}") == Decimal("0.04")
    finally:
        repo.close()


def test_real_fees_refuse_simulation_fill_cutoff(day_pnl_repo: ClerkSqliteRepository) -> None:
    with pytest.raises(ValueError, match="simulated custody"):
        custody_fee_attribution(day_pnl_repo._conn, now_ms=NOON, simulated_fill_cutoff_ms=YESTERDAY_NOON)
