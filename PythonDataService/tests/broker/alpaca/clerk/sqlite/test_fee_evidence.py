"""Custody-fenced fee attribution survives fresh observations and mirror replay."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.sqlite.budget_projection import _external_cash_claim
from app.broker.alpaca.clerk.sqlite.fee_evidence import (
    FEE_EVIDENCE_KIND,
    FEE_EVIDENCE_MAX_AGE_MS,
    FeeEvidenceFacts,
    custody_fee_attribution,
    record_fee_evidence,
)
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerActivity
from app.services.session_authority import et_minute_of_day_ms
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms
from tests.broker.alpaca.clerk.sqlite.conftest import (
    DAY_PNL_ACCOUNT_ID,
    DAY_PNL_SID,
    NOON,
    TODAY_OPEN,
    YESTERDAY_NOON,
    YESTERDAY_OPEN,
    _accept_day_pnl_enter,
    _append_day_pnl_slice,
    _clock_at,
    initialize_day_pnl_repo,
)


def _activity(key: str, kind: str, at: int, amount: float = 0) -> BrokerActivity:
    """A non-trade row as the adapter maps it: only its date, at ET midnight."""
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
        occurred_at_ms=et_midnight_ms(et_date_at_ms(at)),
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
    older = _outside_fill("older", YESTERDAY_NOON - 86_400_000)
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
    window = [_outside_fill(f"row-{index}", YESTERDAY_NOON - 86_400_000 + index) for index in range(300)]
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
    first = _outside_fill("older", YESTERDAY_NOON - 86_400_000)
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
    # Only a trade row proves how far a read reached (#2557).
    record_fee_evidence(repo, [_outside_fill("older", YESTERDAY_NOON - 86_400_000)], checked_at_ms=NOON)
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
    # A read with no trade row records the #2550 shape byte for byte (#2557).
    assert all(
        '"oldest_trade_at_ms"' not in row["facts_json"] for row in original if row["transition_kind"] == FEE_EVIDENCE_KIND
    )
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
    assert await FeeEvidenceSync(repo=day_pnl_repo, read=Read(), intake=ReentrantAsyncLock()).tick()
    assert day_pnl_repo.fee_attribution(now_ms=NOON).known


async def test_a_malformed_activity_row_refuses_one_tick_instead_of_ending_the_producer(
    day_pnl_repo, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One malformed Alpaca row once ended ``clerk-fee-evidence`` for the process's life (#2627).

    The adapter's raw ``KeyError`` escaped the producer's refusal list, so the
    task died and fee coverage aged out with nothing retrying it.
    """
    import asyncio
    import logging

    from app.broker.alpaca.broker import AlpacaBroker
    from app.broker.alpaca.clerk.sqlite import fee_evidence_sync
    from app.broker.alpaca.clerk.sqlite.fee_evidence_sync import FeeEvidenceSync
    from app.broker.contract.errors import BrokerEvidenceUnavailable

    class _Client:
        """First read: a row with no ``activity_type``; then a clean, complete history."""

        def __init__(self) -> None:
            self.reads = 0
            self.retried = asyncio.Event()

        async def list_activities(self, *, limit: int, page_token: str | None = None) -> list[dict]:
            self.reads += 1
            if self.reads == 1:
                return [{"id": "fee-1", "date": "2026-07-21", "net_amount": "-0.01"}]
            if self.reads == 2:
                return []
            # The producer survived into a later tick; park it until stop().
            self.retried.set()
            await asyncio.Event().wait()
            return []

    real_sleep = asyncio.sleep
    monkeypatch.setattr(fee_evidence_sync.asyncio, "sleep", lambda _seconds: real_sleep(0))
    _seed(day_pnl_repo)
    client = _Client()
    sync = FeeEvidenceSync(repo=day_pnl_repo, read=AlpacaBroker(client=client), intake=ReentrantAsyncLock())  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING, logger=fee_evidence_sync.__name__):
        sync.start()
        try:
            await asyncio.wait_for(client.retried.wait(), timeout=5)
        finally:
            await sync.stop()

    refused = [record for record in caplog.records if record.name == fee_evidence_sync.__name__]
    assert refused and refused[0].exc_info is not None
    assert isinstance(refused[0].exc_info[1], BrokerEvidenceUnavailable)
    assert day_pnl_repo.fee_attribution(now_ms=NOON).known


class _PagedHistory:
    """Newest-first provider history read like the adapter: 3 pages of 100."""

    def __init__(self, rows: list[BrokerActivity]) -> None:
        self.rows = rows
        self.tokens: list[str | None] = []
        # Reads (by the token they start at) whose provider cursor is lost
        # once: the page repeats its token or has no id (``broker.py``).
        self.lose_cursor_once: set[str | None] = set()

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
        if page_token in self.lose_cursor_once:
            self.lose_cursor_once.discard(page_token)
            return BrokerActivityEvidence(activities=read, history_complete=False)
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
    first = FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock())
    for sync in (first, first, FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock())):
        await sync.tick()
        known.append(repo.fee_attribution(now_ms=NOON).known)
    assert known == [False, False, True]
    # A fresh producer (restart) resumes from the retained cursor.
    assert history.tokens == [None, "row-299", None, "row-599", None, "row-899"]
    await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
    assert history.tokens[-1:] == [None] and repo.fee_attribution(now_ms=NOON).known


async def test_backfill_keeps_an_uncovered_fill_day_fail_closed(day_pnl_repo) -> None:
    """Relevant days stay refused until a linked read reaches them."""
    repo = day_pnl_repo
    _seed(repo)
    head = [_activity(f"row-{index}", "CSD", NOON - (index + 1) * 1_000) for index in range(300)]
    assert record_fee_evidence(repo, head, checked_at_ms=NOON, next_page_token="row-299")
    assert not repo.fee_attribution(now_ms=NOON).known
    # A read that did not resume at the retained cursor proves no contiguity.
    stray = _outside_fill("stray", YESTERDAY_NOON - 86_400_000)
    record_fee_evidence(repo, [stray], checked_at_ms=NOON, history_complete=True, page_token="not-the-cursor")
    assert not repo.fee_attribution(now_ms=NOON).known
    # The linked continuation covers the day; later heads never uncover it.
    assert record_fee_evidence(repo, [stray], checked_at_ms=NOON, page_token="row-299", next_page_token="stray")
    assert repo.fee_attribution(now_ms=NOON).known
    record_fee_evidence(repo, head, checked_at_ms=NOON + 15_000, next_page_token="row-299")
    assert repo.fee_attribution(now_ms=NOON + 15_000).known


def _burst(count: int) -> list[BrokerActivity]:
    """``count`` outside buys filled today before noon, newest first."""
    return [_outside_fill(f"new-{index}", NOON - (index + 1) * 1_000) for index in range(count)]


def _before_floor(count: int) -> list[BrokerActivity]:
    """``count`` outside buys the day before custody's floor day, newest first."""
    return [_outside_fill(f"old-{index}", YESTERDAY_NOON - 86_400_000 - index * 1_000) for index in range(count)]


async def _record_head_read(repo: ClerkSqliteRepository, history: _PagedHistory) -> None:
    """One newest-first read recorded as the producer records it, with no walk after."""
    head = await history.read_activity_evidence()
    record_fee_evidence(
        repo, head.activities, checked_at_ms=NOON,
        history_complete=head.history_complete, next_page_token=head.next_page_token,
    )


async def test_fee_in_a_gap_between_newest_first_reads_refuses_until_the_gap_walk_reads_it(day_pnl_repo) -> None:
    """Two head reads that never overlap leave the rows between them unread (#2557).

    Coverage came from the oldest row ever read, so the fill day inside the
    gap counted as covered and the fee posted there was silently left out.
    The day now refuses until the walk from the newer read's cursor re-reads
    history custody retained -- joining it well short of the history's end --
    and the fee is then attributed once.
    """
    from app.broker.alpaca.clerk.sqlite.fee_evidence_sync import FeeEvidenceSync

    repo = day_pnl_repo
    _seed(repo)
    bot = f"bot:{DAY_PNL_SID}"
    old = _before_floor(400)
    history = _PagedHistory(old)
    await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
    assert repo.fee_attribution(now_ms=NOON).known
    # 350 rows arrive before the next poll. The fill day's fee posted after
    # that day ended and sorts at its start, under today's fills.
    fee = _activity("gap-fee", "FEE", YESTERDAY_NOON, -0.05)
    history.rows = [*_burst(349), fee, *old]
    await _record_head_read(repo, history)
    gapped = repo.fee_attribution(now_ms=NOON)
    assert not gapped.known, f"the unread fee was left out of {gapped.total_for(bot)}"
    assert "The broker activity read does not cover this fee day." in gapped.unresolved
    await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
    assert history.tokens == [None, None, None, "new-299"]
    assert not _fee_records(repo)[-1].history_complete
    filled = repo.fee_attribution(now_ms=NOON)
    assert filled.known, filled.unresolved
    assert [(share.charge_id, share.state, share.amount) for share in filled.shares if share.subject_id == bot] == [
        ("gap-fee", "observed", Decimal("0.05"))
    ]
    # Later polls re-read the fee's neighbours; it is still counted once.
    await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
    assert history.tokens[-1:] == [None]
    assert repo.fee_attribution(now_ms=NOON).total_for(bot) == Decimal("0.05")
    assert [row.activity_id for record in _fee_records(repo) for row in record.activities].count("gap-fee") == 1


async def test_a_date_only_row_in_a_newer_read_cannot_prove_it_met_retained_history(day_pnl_repo) -> None:
    """A midnight stamp cannot join two reads across a gap (#2557 review).

    A date-only row carries only ET midnight of its date and may post long
    after it. Among the newest rows it once pulled the read's oldest instant
    back past the history custody retained, so the gap below those rows was
    never walked. Only trade rows place a read in the newest-first order.
    """
    from app.broker.alpaca.clerk.sqlite.fee_evidence_sync import FeeEvidenceSync

    repo = day_pnl_repo
    _seed(repo)
    afternoon = [_outside_fill(f"old-{index}", YESTERDAY_NOON + 3_600_000 - index * 1_000) for index in range(50)]
    history = _PagedHistory(afternoon)
    await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
    assert repo.fee_attribution(now_ms=NOON).known
    burst = _burst(400)
    history.rows = [*burst[:150], _activity("fee", "FEE", YESTERDAY_NOON, -0.05), *burst[150:], *afternoon]
    await _record_head_read(repo, history)
    gapped = repo.fee_attribution(now_ms=NOON)
    assert not gapped.known, "a date-only row joined the newest read to retained history"
    assert "The broker activity read does not cover this fee day." in gapped.unresolved
    await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
    assert history.tokens == [None, None, None, "new-298"]
    assert repo.fee_attribution(now_ms=NOON).known


async def test_outside_short_sale_in_a_gap_is_read_instead_of_counted_covered(day_pnl_repo) -> None:
    """The #2550 re-check reproduction: a complete read, then 350 new rows (#2557).

    The next head read proved no contiguity with the first, the walk never
    ran, and the outside short sale in the gap never reached custody.
    """
    from app.broker.alpaca.clerk.sqlite.fee_evidence_sync import FeeEvidenceSync

    repo = day_pnl_repo
    _seed(repo)
    old = [_activity(f"old-{index}", "CSD", YESTERDAY_NOON - 86_400_000 - index) for index in range(50)]
    history = _PagedHistory(old)
    await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
    assert repo.fee_attribution(now_ms=NOON).known
    burst = _burst(349)
    short = _outside_fill("gap-short", NOON - 320_500, side="sell_short", order="console-short")
    history.rows = [*burst[:320], short, *burst[320:], *old]
    await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
    result = repo.fee_attribution(now_ms=NOON)
    assert not result.known, "the short sale in the gap was never read"
    assert history.tokens == [None, None, "new-299"]
    assert "Account fill coverage is incomplete. Reconcile account executions before deploying." in result.unresolved


async def test_closing_a_gap_hands_back_to_the_history_walk_where_it_stopped(day_pnl_repo) -> None:
    """A gap opened mid-backfill is walked first; the history walk keeps its place (#2557)."""
    from app.broker.alpaca.clerk.sqlite.fee_evidence_sync import FeeEvidenceSync

    repo = day_pnl_repo
    _seed(repo)
    today = [_outside_fill(f"row-{index}", NOON - 1_000_000 - (index + 1) * 1_000) for index in range(900)]
    older = [_activity(f"row-{900 + index}", "CSD", YESTERDAY_NOON - 86_400_000 - index) for index in range(100)]
    history = _PagedHistory([*today, *older])
    await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
    history.rows = [*_burst(400), *today, *older]
    known = []
    for _ in range(3):
        await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
        known.append(repo.fee_attribution(now_ms=NOON).known)
    # The gap walk read down to row-199; the history walk resumes at row-599.
    assert history.tokens == [None, "row-299", None, "new-299", None, "row-599", None, "row-899"]
    assert known == [False, False, True]


async def test_a_walk_read_that_loses_its_cursor_resumes_from_the_next_head_read(day_pnl_repo) -> None:
    """A walk read can end unfinished with no cursor (#2557 review).

    The provider repeats its token or gives a row no id. Joining the next
    head read must keep a cursor to walk from -- the head read's own, which
    re-reads the overlap and then extends below it -- instead of taking the
    deeper run's missing one and refusing the fill day forever.
    """
    from app.broker.alpaca.clerk.sqlite.fee_evidence_sync import FeeEvidenceSync

    repo = day_pnl_repo
    _seed(repo)
    today = [_activity(f"row-{index}", "CSD", NOON - (index + 1) * 1_000) for index in range(900)]
    older = [_activity(f"row-{900 + index}", "CSD", YESTERDAY_NOON - 86_400_000 - index) for index in range(100)]
    history = _PagedHistory([*today, *older])
    history.lose_cursor_once.add("row-299")
    known = []
    for _ in range(4):
        await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
        known.append(repo.fee_attribution(now_ms=NOON).known)
    assert history.tokens == [None, "row-299", None, "row-299", None, "row-599", None, "row-899"]
    assert known == [False, False, False, True]


async def test_a_gap_opened_by_a_cursorless_read_is_walked_from_the_next_head_read(day_pnl_repo) -> None:
    """A head read with no cursor cannot walk its own gap; the next one does (#2557 review)."""
    from app.broker.alpaca.clerk.sqlite.fee_evidence_sync import FeeEvidenceSync

    repo = day_pnl_repo
    _seed(repo)
    old = [_activity(f"old-{index}", "CSD", YESTERDAY_NOON - 86_400_000 - index) for index in range(50)]
    history = _PagedHistory(old)
    await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
    history.rows = [*_burst(400), *old]
    history.lose_cursor_once.add(None)
    known = []
    for _ in range(2):
        await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
        known.append(repo.fee_attribution(now_ms=NOON).known)
    assert history.tokens == [None, None, None, "new-299"]
    assert known == [False, True]


def test_days_the_history_walk_has_not_reached_refuse_with_no_known_row(tmp_path) -> None:
    """Every custody day the walk has not reached is unproven (#2557 review).

    Custody began 09-01 with no fill of its own and the newest read reaches
    back only to 09-05, so an outside short sale on 09-03 is still unread.
    No known row opened those days, so they once counted as covered.
    """
    genesis = et_minute_of_day_ms(date(2026, 9, 1), 12 * 60)
    repo = ClerkSqliteRepository.initialize(account_id="PA-unreached", artifacts_root=tmp_path, clock=_clock_at(genesis))
    try:
        # Fills from 09-06 on; the read's oldest rows are deposits dated 09-05.
        fills = [_outside_fill(f"recent-{index}", NOON - (index + 1) * 720_000) for index in range(250)]
        deposits = [_activity(f"deposit-{index}", "CSD", et_minute_of_day_ms(date(2026, 9, 5), 12 * 60)) for index in range(50)]
        record_fee_evidence(repo, [*fills, *deposits], checked_at_ms=NOON, next_page_token="deposit-49")
        unreached = custody_fee_attribution(repo._conn, now_ms=NOON, evidence_checked_at_ms=NOON)
        assert not unreached.known, "days the walk has not reached counted as covered"
        assert "The broker activity read does not cover this fee day." in unreached.unresolved
        # The walk reaches the short sale custody would otherwise never see.
        short = _outside_fill(
            "unread-short", et_minute_of_day_ms(date(2026, 9, 3), 12 * 60), side="sell_short", order="console-short"
        )
        record_fee_evidence(repo, [short], checked_at_ms=NOON, history_complete=True, page_token="deposit-49")
        walked = custody_fee_attribution(repo._conn, now_ms=NOON, evidence_checked_at_ms=NOON)
        assert "The broker activity read does not cover this fee day." not in walked.unresolved
        assert "Account fill coverage is incomplete. Reconcile account executions before deploying." in walked.unresolved
    finally:
        repo.close()


async def _record_master_era_head_read(repo: ClerkSqliteRepository, history: _PagedHistory) -> None:
    """One head read recorded as the #2550 writer did: its new rows, no oldest trade row."""
    from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
    from app.broker.alpaca.clerk.sqlite.models import TransitionInput

    head = await history.read_activity_evidence()
    retained = {row.activity_id for record in _fee_records(repo) for row in record.activities}
    repo.append_transition(TransitionInput(
        transition_kind=FEE_EVIDENCE_KIND, custody_owner="ACCOUNT_CLERK", execution_authority="ACCOUNT_CLERK",
        operation_state="succeeded", clerk_observed_at_ms=NOON, summary_code=FEE_EVIDENCE_KIND,
        facts_json=canonicalize({
            "activities": [row.model_dump(mode="json") for row in head.activities if row.activity_id not in retained],
            "checked_at_ms": NOON,
            "history_complete": head.history_complete,
            "next_page_token": head.next_page_token,
            "oldest_occurred_at_ms": min(row.occurred_at_ms for row in head.activities),
            "page_token": None,
        }),
    ))


async def test_records_written_before_the_trade_reach_fact_refuse_until_a_read_rejoins_them(day_pnl_repo) -> None:
    """Deploy over records the #2550 writer left (#2557 review).

    Those records carry no oldest trade row, so a head read that added rows
    over retained ones proves only its new rows' reach and never meets the
    history below it: its days refuse. The first head read after deploy
    carries the fact, rejoins that history and releases them.
    """
    from app.broker.alpaca.clerk.sqlite.fee_evidence_sync import FeeEvidenceSync

    repo = day_pnl_repo
    _seed(repo)
    old = _before_floor(400)
    history = _PagedHistory(old)
    await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
    assert '"oldest_trade_at_ms"' in repo.custody_transitions()[-1]["facts_json"]
    history.rows = [*_burst(5), *old]
    await _record_master_era_head_read(repo, history)
    assert [row.activity_id for row in _fee_records(repo)[-1].activities] == [f"new-{index}" for index in range(5)]
    assert not repo.fee_attribution(now_ms=NOON).known
    await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
    assert history.tokens[-1:] == [None]
    assert repo.fee_attribution(now_ms=NOON).known


def _fills(prefix: str, count: int, newest_ms: int) -> list[BrokerActivity]:
    """``count`` outside buys a second apart, newest first."""
    return [_outside_fill(f"{prefix}-{index}", newest_ms - index * 1_000) for index in range(count)]


async def test_a_record_without_a_trade_row_never_sends_the_walk_past_custodys_floor(day_pnl_repo) -> None:
    """Deploy over #2550 records, one of which added only a FEE (#2557 re-check).

    That record holds no trade row, so the runs around it could never join:
    the walk from the first head read after deploy never met the history
    retained below it, ran through thousands of rows dated before custody's
    floor to the provider's end of history, and refused every tick meanwhile.
    It now heals as soon as the walk reaches the history retained below, the
    FEE is attributed once, and nothing before the floor is read.
    """
    from app.broker.alpaca.clerk.sqlite.fee_evidence_sync import FeeEvidenceSync

    repo = day_pnl_repo
    _seed(repo)
    bot = f"bot:{DAY_PNL_SID}"
    morning = _fills("morning", 200, TODAY_OPEN - 3_600_000)
    polled = _fills("polled", 5, TODAY_OPEN)
    fee = _activity("late-fee", "FEE", YESTERDAY_NOON, -0.05)
    before = _before_floor(3_000)
    history = _PagedHistory([])
    # The #2550 writer: a head read reaching past the floor, a poll that added
    # five fills, then a poll whose only new row was the fill day's FEE.
    for rows in ([*morning, *before], [*polled, *morning, *before], [fee, *polled, *morning, *before]):
        history.rows = rows
        await _record_master_era_head_read(repo, history)
    assert [row.activity_id for row in _fee_records(repo)[-1].activities] == ["late-fee"]
    # Deployed; 700 fills arrive before the first poll.
    history.rows = [*_burst(700), fee, *polled, *morning, *before]
    history.tokens.clear()
    known = []
    for _ in range(3):
        await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
        known.append(repo.fee_attribution(now_ms=NOON).known)
    assert known == [False, True, True]
    # Two gap walks reach the history retained below; no read starts before the floor.
    assert history.tokens == [None, "new-299", None, "new-599", None]
    assert repo.fee_attribution(now_ms=NOON).total_for(bot) == Decimal("0.05")
    assert [row.activity_id for record in _fee_records(repo) for row in record.activities].count("late-fee") == 1


async def test_a_first_read_without_a_trade_row_never_sends_the_walk_past_custodys_floor(day_pnl_repo) -> None:
    """The first #2550 read held only deposits, so no later read can meet it (#2557 re-check).

    Their dates once carried its reach past the floor; they prove nothing,
    and with no trade row nothing can overlap it. Once a newer head read
    starts from the top it is dropped, so the walk from that read stops at
    custody's floor instead of running to the end of history.
    """
    from app.broker.alpaca.clerk.sqlite.fee_evidence_sync import FeeEvidenceSync

    repo = day_pnl_repo
    _seed(repo)
    deposits = [_activity(f"deposit-{index}", "CSD", NOON - (index + 1) * 1_800_000) for index in range(300)]
    before = _before_floor(3_000)
    history = _PagedHistory([*deposits, *before])
    await _record_master_era_head_read(repo, history)
    history.rows = [*_burst(700), *deposits, *before]
    history.tokens.clear()
    known = []
    for _ in range(4):
        await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
        known.append(repo.fee_attribution(now_ms=NOON).known)
    assert known == [False, False, True, True]
    assert history.tokens == [None, "new-299", None, "new-599", None, "deposit-199", None]


async def test_a_date_only_row_never_proves_how_far_a_read_reached(tmp_path) -> None:
    """A FEE stamped at an earlier ET midnight cannot carry a read past custody's floor (#2557 re-check).

    Custody began today. The head read reaches fills back to 04:10 and ends
    in the previous session's late FEE, stamped at that day's ET midnight.
    That stamp once counted as the read's reach: the floor was reached, no
    walk ran, and the rows before 04:10 -- an outside short sale among them
    -- were never read.
    """
    from app.broker.alpaca.clerk.sqlite.fee_evidence_sync import FeeEvidenceSync

    today = date(2026, 9, 8)
    repo = ClerkSqliteRepository.initialize(account_id="PA-midnight-reach", artifacts_root=tmp_path, clock=_clock_at(NOON))
    try:
        since_0410 = _fills("since-0410", 299, et_minute_of_day_ms(today, 4 * 60 + 10) + 298_000)
        late_fee = _activity("late-fee", "FEE", YESTERDAY_NOON, -0.05)
        short = _outside_fill("early-short", et_minute_of_day_ms(today, 2 * 60), side="sell_short", order="console-short")
        early = [*_fills("early", 10, et_minute_of_day_ms(today, 4 * 60)), short, *_fills("earlier", 10, et_minute_of_day_ms(today, 60))]
        history = _PagedHistory([*since_0410, late_fee, *early, *_fills("friday", 20, YESTERDAY_NOON)])
        await _record_head_read(repo, history)
        unread = repo.fee_attribution(now_ms=NOON)
        assert not unread.known, "a midnight stamp counted as reaching custody's floor"
        assert "The broker activity read does not cover this fee day." in unread.unresolved
        await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
        assert history.tokens == [None, None, "late-fee"]
        read = repo.fee_attribution(now_ms=NOON)
        assert "The broker activity read does not cover this fee day." not in read.unresolved
        assert "Account fill coverage is incomplete. Reconcile account executions before deploying." in read.unresolved
    finally:
        repo.close()


async def test_restarted_producer_resumes_an_unfinished_gap_walk_and_older_days_stay_covered(
    tmp_path, day_pnl_clock
) -> None:
    """A gap never shrinks what was proven, and its walk survives a restart (#2557).

    The gap lies inside today, after the newest trade custody retained: the
    fill day before it stays covered throughout, today refuses (with no fill
    of custody's own) until the walk reaches retained history, and a
    restarted Clerk continues that walk from the cursor its hash chain kept.
    """
    from app.broker.alpaca.clerk.sqlite.fee_evidence_sync import FeeEvidenceSync

    def fill_day(repo: ClerkSqliteRepository):
        return custody_fee_attribution(
            repo._conn, now_ms=NOON, evidence_checked_at_ms=NOON,
            from_ms=et_midnight_ms(date(2026, 9, 4)), to_ms=et_midnight_ms(date(2026, 9, 5)),
        )

    repo = initialize_day_pnl_repo(tmp_path, day_pnl_clock)
    try:
        _seed(repo)
        this_morning = [_outside_fill(f"morning-{index}", TODAY_OPEN - index * 1_000) for index in range(20)]
        older = [_activity(f"old-{index}", "CSD", YESTERDAY_NOON - 86_400_000 - index) for index in range(50)]
        history = _PagedHistory([*this_morning, *older])
        await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
        assert repo.fee_attribution(now_ms=NOON).known
        # 700 rows arrive after this morning's before the next poll: one tick
        # reads the newest 300 and walks 300 more, still short of the gap's end.
        history.rows = [*_burst(700), *this_morning, *older]
        await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
        gapped = repo.fee_attribution(now_ms=NOON)
        assert not gapped.known, "today's unread rows were counted as covered"
        assert gapped.unresolved == ("The broker activity read does not cover this fee day.",)
        assert history.tokens == [None, None, "new-299"]
        covered = fill_day(repo)
        assert covered.known and covered.total_for(f"bot:{DAY_PNL_SID}") == Decimal("0.01")
    finally:
        repo.close()
    restarted = ClerkSqliteRepository.open(account_id=DAY_PNL_ACCOUNT_ID, artifacts_root=tmp_path, clock=day_pnl_clock)
    try:
        await FeeEvidenceSync(repo=restarted, read=history, intake=ReentrantAsyncLock()).tick()
        assert history.tokens[-2:] == [None, "new-599"]
        assert restarted.fee_attribution(now_ms=NOON).known
        assert fill_day(restarted).total_for(f"bot:{DAY_PNL_SID}") == Decimal("0.01")
    finally:
        restarted.close()


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
    # Outside fills: only a trade row proves how far the head read reached (#2557).
    recent = [_outside_fill(f"row-{index}", NOON - (index + 1) * 300_000) for index in range(600)]
    older = [_activity(f"old-{index}", "CSD", ancient.occurred_at_ms - (index + 1) * 1_000) for index in range(10)]
    history = _PagedHistory([*recent, ancient, *older])
    known = []
    for _ in range(3):
        await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
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
        await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
        known.append(repo.fee_attribution(now_ms=NOON).known)
    assert known == [False, False, True, True]
    assert history.tokens == [None, "row-299", None, "row-599", None, "row-899", None]
    assert ancient.activity_id in {row.activity_id for record in _fee_records(repo) for row in record.activities}
    assert not repo.fee_attribution(now_ms=NOON).external_fills


def test_outside_fills_attribute_from_the_custody_floor_day_on(day_pnl_repo) -> None:
    """Only days before the floor leave fee attribution; the floor day still
    claims its fees (#2550). The execution itself predates custody's genesis
    instant, so for money it is its order's quantity only (H35)."""
    repo = day_pnl_repo
    _seed(repo)
    on_floor = _outside_fill("floor-day-buy", YESTERDAY_OPEN, order="floor-day-order")
    before_floor = _outside_fill("day-before-buy", YESTERDAY_NOON - 86_400_000, order="day-before-order")
    record_fee_evidence(repo, [on_floor, before_floor], checked_at_ms=NOON, history_complete=True)
    result = repo.fee_attribution(now_ms=NOON)
    assert result.known
    assert result.external_fills == ()
    assert result.pre_custody_quantities == {"floor-day-order": Decimal(1)}
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
        await FeeEvidenceSync(repo=repo, read=history, intake=ReentrantAsyncLock()).tick()
    assert history.tokens == [None, "row-299", None, "row-599", None, "row-899", None]
    result = repo.fee_attribution(now_ms=NOON)
    assert result.known
    # Both executions predate custody: they complete the order, never price it.
    assert result.external_fills == ()
    assert result.pre_custody_quantities == {"gtc-order": Decimal(2)}


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
    later = _outside_fill("gtc-later", NOON + 1_000, order="gtc-order")
    earlier = _outside_fill("gtc-earlier", et_minute_of_day_ms(earlier_day, 12 * 60), order="gtc-order")
    record_fee_evidence(repo, [later, *unrelated, earlier], checked_at_ms=NOON, history_complete=True)
    result = repo.fee_attribution(now_ms=NOON)
    assert result.known, result.unresolved
    assert [fill.fill_id for fill in result.external_fills] == ["gtc-later"]
    assert result.pre_custody_quantities == {"gtc-order": Decimal(1)}
    # Only the execution on custody's day is priced.
    assert result.total_for("external:gtc-order") == Decimal("0.01")
    # Both executions reach the claim's exact filled-quantity check (2 shares);
    # only the custody-era one is a purchase the cash may not show yet.
    assert _external_cash_claim(repo._conn, result, seen_before_ms=NOON) == Decimal(100)
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


async def test_guard_reads_one_activity_type_through_the_walk_and_its_fallback_alike() -> None:
    from app.broker.alpaca.clerk.sqlite.broker_port_guard import guard_broker_read_port
    from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
    from app.broker.contract.models import BrokerActivityEvidence

    class Paged:
        def __init__(self) -> None:
            self.activity_types: list[str | None] = []

        async def read_activity_evidence(
            self, *, page_token: str | None = None, after_ms: int | None = None, activity_type: str | None = None
        ):
            self.activity_types.append(activity_type)
            return BrokerActivityEvidence(activities=[], history_complete=True)

    class NewestOnly:
        def __init__(self) -> None:
            self.activity_types: list[str | None] = []

        async def list_activities(
            self, *, after_ms: int | None = None, limit: int = 100, activity_type: str | None = None
        ) -> list[BrokerActivity]:
            self.activity_types.append(activity_type)
            return []

    for inner in (Paged(), NewestOnly()):
        guarded = guard_broker_read_port(inner, intake=ReentrantAsyncLock())  # type: ignore[arg-type]
        await guarded.read_activity_evidence(after_ms=NOON, activity_type="FILL")
        assert inner.activity_types == ["FILL"]


def test_future_checked_time_is_not_fresh_risk_evidence(day_pnl_repo) -> None:
    record_fee_evidence(day_pnl_repo, [], checked_at_ms=NOON + 1, history_complete=True)
    result = day_pnl_repo.fee_attribution(now_ms=NOON)
    assert not result.known
    assert any("stale" in reason for reason in result.unresolved)
    # A fresh delivery after a clock correction must replace the bad freshness
    # stamp, even though its economic rows are unchanged (and append nothing).
    assert not record_fee_evidence(day_pnl_repo, [], checked_at_ms=NOON, history_complete=True)
    assert day_pnl_repo.fee_attribution(now_ms=NOON).known


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


def test_real_fees_refuse_simulation_fill_cutoff(day_pnl_repo: ClerkSqliteRepository) -> None:
    with pytest.raises(ValueError, match="simulated custody"):
        custody_fee_attribution(day_pnl_repo._conn, now_ms=NOON, simulated_fill_cutoff_ms=YESTERDAY_NOON)
