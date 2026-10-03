"""``GET /api/brokers/alpaca/attention`` — one lane's bell items (#2228).

The lane answers from its own custody ledger: every unresolved uncertainty,
keyed by the stable uncertainty id a bell dedupes and clears by. A lane with
no active runtime answers empty — *unknown* is the coordinator's judgment
when this read cannot be reached, never this route's.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient

from app.broker.alpaca.broker import ALPACA_EXTENDED_HOURS_WINDOW
from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.sqlite.budget_authority import commit_budget_authority_cutover
from app.broker.alpaca.clerk.sqlite.exit_recovery import RecoveryResult, record_exit_recovery
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    raise_uncertainty,
    resolve_exit_not_flat_uncertainty,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXIT_NOT_FLAT_REASON_CODE,
)
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.main import app
from app.utils.timestamps import to_ms_utc

ACCOUNT = "PA-BELL-1"
_T0 = 1_788_040_000_000  # a fixed int64 ms UTC
_ET = ZoneInfo("America/New_York")


class _Clock:
    """One steppable repo clock so two episodes observe at distinct stamps."""

    def __init__(self) -> None:
        self.now_ms = _T0

    def __call__(self) -> int:
        return self.now_ms


@pytest.fixture(autouse=True)
def _no_runtime() -> Iterator[None]:
    set_active_clerk_runtime(None)
    yield
    set_active_clerk_runtime(None)


def _budgeted(repo: ClerkSqliteRepository) -> ClerkSqliteRepository:
    """An account on budgets: it raises no "switch to budgets" line of its own."""
    commit_budget_authority_cutover(repo, actor="owner", reviewed_token="empty-account", stop_receipt="no-runs")
    return repo


def _raise_exit_not_flat(
    repo: ClerkSqliteRepository, *, strategy_instance_id: str, symbol: str
) -> None:
    raise_uncertainty(
        repo,
        strategy_instance_id=strategy_instance_id,
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        headline="This bot's exit has not flattened its position",
        explanation="The exit's attributed position is not flat.",
        operator_impact="New exposure for this bot is blocked.",
        next_step="Open the bot's flatten ticket.",
        cause_facts={"symbol": symbol, "attributed_qty": 10.0},
        severity="blocking",
    )


def _repo_with_two_episodes(
    tmp_path: Path, *, start_ms: int = _T0
) -> tuple[ClerkSqliteRepository, _Clock]:
    clock = _Clock()
    clock.now_ms = start_ms
    repo = _budgeted(ClerkSqliteRepository.initialize(
        account_id=ACCOUNT, artifacts_root=tmp_path, clock=clock
    ))
    for strategy_instance_id, symbol in (("ema-1", "SPY"), ("ema-2", "QQQ")):
        repo.register_strategy_instance(
            strategy_instance_id=strategy_instance_id, symbol=symbol, config_hash="h1", config_json='{"mode":"trade","quantity":1,"carryover_policy":"FORBID"}'
        )
        _raise_exit_not_flat(
            repo, strategy_instance_id=strategy_instance_id, symbol=symbol
        )
        clock.now_ms += 1_000
    return repo, clock


async def test_a_lane_with_no_runtime_answers_empty_never_unknown() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/alpaca/attention")

    assert response.status_code == 200
    assert response.json() == {"account_id": None, "items": []}


async def test_items_expose_the_uncertainty_id_symbol_and_order(
    tmp_path: Path,
) -> None:
    repo, _clock = _repo_with_two_episodes(tmp_path)
    set_active_clerk_runtime(
        ActiveClerkRuntime(
            authority_kind="sqlite", _sqlite_repository=repo, account_id=ACCOUNT
        )
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/alpaca/attention")

    assert response.status_code == 200
    body = response.json()
    assert body["account_id"] == ACCOUNT
    items = body["items"]
    assert [item["strategy_instance_id"] for item in items] == ["ema-1", "ema-2"], (
        "oldest observation first — the bell lists what has been waiting longest on top"
    )
    first, second = items
    # The bell's stable identity is the ledger's uncertainty id, so an item
    # clears exactly when its episode resolves.
    assert first["condition_id"] == repo.active_uncertainties()[0]["uncertainty_id"]
    assert first["reason_code"] == EXIT_NOT_FLAT_REASON_CODE
    assert first["kind"] == "exit"
    # An exit that has not flattened is fixed from its bot's own page.
    assert first["action"] == {"label": "Open bot", "destination": "bot"}
    assert first["severity"] == "blocking"
    assert first["symbol"] == "SPY"
    assert first["headline"] == "This bot's exit has not flattened its position"
    assert second["symbol"] == "QQQ"


async def test_an_item_clears_exactly_when_its_episode_resolves(
    tmp_path: Path,
) -> None:
    repo, _clock = _repo_with_two_episodes(tmp_path)
    set_active_clerk_runtime(
        ActiveClerkRuntime(
            authority_kind="sqlite", _sqlite_repository=repo, account_id=ACCOUNT
        )
    )

    assert resolve_exit_not_flat_uncertainty(
        repo, strategy_instance_id="ema-1", evidence_refs=("order:o1",)
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/alpaca/attention")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["strategy_instance_id"] for item in items] == ["ema-2"]


async def test_other_brokers_are_404() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/ibkr/attention")

    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "lane_attention_unsupported_broker"


async def test_items_carry_the_next_attempt_as_the_one_projection_says_it(
    tmp_path: Path,
) -> None:
    """The bell uses the same authority-owned eligibility as the desk and panel.

    An elapsed time remains eligibility without a guessed retry status. A
    closed session advances it to this authority's next permitted window.
    An unreadable episode still rings without a symbol or a time.
    """
    wednesday_after_hours = to_ms_utc(datetime(2026, 9, 2, 17, 0, tzinfo=_ET))
    repo, clock = _repo_with_two_episodes(tmp_path, start_ms=wednesday_after_hours)
    repo.register_strategy_instance(strategy_instance_id="ema-3", symbol="IWM", config_hash="h1", config_json='{"mode":"trade","quantity":1,"carryover_policy":"FORBID"}')
    for strategy_instance_id, symbol, next_attempt_at_ms in (
        ("ema-1", "SPY", wednesday_after_hours + 3_600_000),
        ("ema-2", "QQQ", wednesday_after_hours - 60_000),
    ):
        raise_uncertainty(
            repo,
            strategy_instance_id=strategy_instance_id,
            reason_code=EXIT_NOT_FLAT_REASON_CODE,
            headline="This bot's exit has not flattened its position",
            explanation="The exit's attributed position is not flat.",
            operator_impact="New exposure for this bot is blocked.",
            next_step="Let the automatic re-drive reduce it.",
            cause_facts={"symbol": symbol, "attributed_qty": 10.0},
            severity="blocking",
            refresh_unchanged=True,

        )
        episode = repo.active_uncertainty(
            scope="CUSTODY_SUBJECT", reason_code=EXIT_NOT_FLAT_REASON_CODE,
            strategy_instance_id=strategy_instance_id,
        )
        record_exit_recovery(
            repo, strategy_instance_id=strategy_instance_id, uncertainty_id=episode["uncertainty_id"],
            result=RecoveryResult("hold", "NO_SESSION_OPEN", "Waiting for the next session.", next_attempt_at_ms),
            evaluated_at_ms=repo.clock(), pass_started_at_ms=repo.clock(),
        )
    _raise_exit_not_flat(repo, strategy_instance_id="ema-3", symbol="IWM")
    repo._conn.execute(
        "UPDATE uncertainties SET facts_json = ? WHERE strategy_instance_id = 'ema-3'",
        ('{"written_by_a_later_schema":true}',),
    )
    repo._conn.commit()
    facade = SqliteAlpacaClerkFacade(
        account_mode="paper",
        repo=repo,
        read=object(),  # type: ignore[arg-type]
        trade=object(),  # type: ignore[arg-type]
        program_leg_policy=ProgramLegPolicy(
            window=ALPACA_EXTENDED_HOURS_WINDOW,
            allowances=ExtendedHoursAllowances(entry_bps=Decimal("10"), exit_bps=Decimal("20")),
        ),
    )
    set_active_clerk_runtime(
        ActiveClerkRuntime(
            authority_kind="sqlite", clerk=facade, _sqlite_repository=repo, account_id=ACCOUNT
        )
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/alpaca/attention")
        clock.now_ms = to_ms_utc(datetime(2026, 9, 2, 20, 30, tzinfo=_ET))
        overnight = await client.get("/api/brokers/alpaca/attention")

    assert response.status_code == 200
    by_sid = {item["strategy_instance_id"]: item for item in response.json()["items"] if item["reason_code"] == "EXIT_NOT_FLAT"}
    assert by_sid["ema-1"]["recovery_status"]["allowed_from_ms"] == wednesday_after_hours + 3_600_000
    assert by_sid["ema-2"]["recovery_status"]["kind"] == "allowed_now"
    assert by_sid["ema-2"]["recovery_status"]["allowed_from_ms"] is None
    unreadable = by_sid["ema-3"]
    assert (
        unreadable["symbol"],
        unreadable["recovery_status"]["allowed_from_ms"],
        unreadable["recovery_status"]["kind"],
        unreadable["recovery_status"]["reason_code"],
    ) == (None, None, "unknown", "RECOVERY_RECORD_UNREADABLE")
    overnight_ema_2 = next(
        item for item in overnight.json()["items"] if item["strategy_instance_id"] == "ema-2" and item["reason_code"] == "EXIT_NOT_FLAT"
    )
    assert overnight_ema_2["recovery_status"]["allowed_from_ms"] is None
    assert overnight_ema_2["recovery_status"]["kind"] == "unknown"
    assert overnight_ema_2["recovery_status"]["last_checked_at_ms"] == by_sid["ema-2"]["recovery_status"]["last_checked_at_ms"]
    assert "next_attempt_overdue" not in overnight_ema_2


@pytest.mark.asyncio
async def test_attention_without_facade_retains_notice_but_cannot_invent_retry_time(tmp_path: Path) -> None:
    clock = _Clock()
    repo = _budgeted(ClerkSqliteRepository.initialize(account_id=ACCOUNT, artifacts_root=tmp_path, clock=clock))
    repo.register_strategy_instance(strategy_instance_id="ema-1", symbol="SPY", config_hash="h1", config_json='{"mode":"trade","quantity":1,"carryover_policy":"FORBID"}')
    raise_uncertainty(
        repo, strategy_instance_id="ema-1", reason_code=EXIT_NOT_FLAT_REASON_CODE,
        headline="Exit is not flat", explanation="Position remains", operator_impact="Entry blocked",
        next_step="Reconcile", cause_facts={"symbol": "SPY"}, severity="blocking",

    )
    set_active_clerk_runtime(ActiveClerkRuntime(
        authority_kind="sqlite", _sqlite_repository=repo, account_id=ACCOUNT,
    ))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/brokers/alpaca/attention")
        item, = response.json()["items"]
        assert item["symbol"] == "SPY"
        assert item["recovery_status"]["allowed_from_ms"] is None
        assert "next_attempt_overdue" not in item
    finally:
        repo.close()


async def test_dead_run_reaches_account_desk_and_bell_without_selling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.engine.live.bot_lifecycle_state import (
        BotDutyOutcome,
        BotLifecyclePhase,
        BotLifecycleStateRecord,
        stable_bot_lifecycle_state_path,
    )
    from app.services.broker_v2_panel import sqlite_roster_status

    clock = _Clock()
    repo = _budgeted(ClerkSqliteRepository.initialize(account_id=ACCOUNT, artifacts_root=tmp_path, clock=clock))
    repo.register_strategy_instance(strategy_instance_id="dead-bot", symbol="SPY", config_hash="h1", config_json='{"mode":"trade","quantity":1,"carryover_policy":"FORBID"}')
    monkeypatch.setattr(sqlite_roster_status, "live_artifacts_root", lambda: tmp_path)
    path = stable_bot_lifecycle_state_path(tmp_path, "dead-bot")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BotLifecycleStateRecord(
        phase=BotLifecyclePhase.OFF_DUTY, active_run_id=None, last_transition_at_ms=clock.now_ms,
        duty_outcome=BotDutyOutcome(kind="CRASHED", reason_code="FEED_DEATH", recorded_at_ms=clock.now_ms, run_id="dead-run"),
    ).model_dump_json())
    # No broker methods exist: these projections must neither sell nor contact it.
    facade = SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=object(), trade=object())
    set_active_clerk_runtime(ActiveClerkRuntime(
        authority_kind="sqlite", clerk=facade, _sqlite_repository=repo, account_id=ACCOUNT,
    ))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            bell = await client.get("/api/brokers/alpaca/attention")
            desk = await client.get(f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT}/snapshot")
        assert bell.status_code == 200
        # The bot is flat and claims nothing, so it is never a line of its
        # own (review A1): the bell asks once for the account to be checked
        # against Alpaca, since nothing has checked it since the crash.
        [line] = bell.json()["items"]
        assert (line["kind"], line["strategy_instance_id"], line["action"]["destination"]) == (
            "out_of_sync", None, "reconcile",
        )
        assert desk.status_code == 200
        # The desk still names the bot's own unverified custody, in the words
        # that send the owner to the bot's own page, never to the broker (H29).
        assert desk.json()["exposure_notices"][0]["label"] == "Position could not be verified"
    finally:
        repo.close()


async def test_corrupt_lifecycle_preserves_sibling_recovery_notices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.engine.live.bot_lifecycle_state import stable_bot_lifecycle_state_path
    from app.services.broker_v2_panel import sqlite_roster_status

    repo, _ = _repo_with_two_episodes(tmp_path)
    monkeypatch.setattr(sqlite_roster_status, "live_artifacts_root", lambda: tmp_path)
    path = stable_bot_lifecycle_state_path(tmp_path, "ema-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken lifecycle")
    facade = SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=object(), trade=object())
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=facade, _sqlite_repository=repo, account_id=ACCOUNT))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            bell = await client.get("/api/brokers/alpaca/attention")
            desk = await client.get(f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT}/snapshot")
        assert bell.status_code == desk.status_code == 200
        items = bell.json()["items"]
        assert {item["strategy_instance_id"] for item in items if item["reason_code"] == "EXIT_NOT_FLAT"} == {"ema-1", "ema-2"}
        [unreadable] = desk.json()["exposure_notices"]
        assert unreadable["strategy_instance_id"] == "ema-1"
        assert unreadable["kind"] == "position_unverified"
        assert "could not be read" in unreadable["explanation"]
        # Hurdle H29: the fix is in the app, never "at the broker".
        [line] = [item for item in items if item["reason_code"] == "LIFECYCLE_UNREADABLE"]
        assert line["headline"] == (
            "ema-1's lifecycle could not be read, so what it holds is unknown. "
            "Reconcile now to re-read the account at Alpaca."
        )
        assert line["action"] == {"label": "Reconcile now", "destination": "reconcile"}
        for text in (line["headline"], unreadable["label"], unreadable["explanation"]):
            assert "at the broker" not in text and "check the broker" not in text.lower()
    finally:
        repo.close()


# ── Home's attention lines (PRD #2560): one headline, one fix each ───────────

_TRADE_CONFIG = '{"mode":"trade","quantity":1,"carryover_policy":"FORBID"}'


def _hold_position(repo: ClerkSqliteRepository, sid: str, quantity: float) -> None:
    repo._conn.execute(
        "INSERT INTO positions (subject_id, strategy_instance_id, symbol, attributed_qty, updated_at_ms) "
        "VALUES (?, ?, 'SPY', ?, ?)",
        (f"bot:{sid}", sid, quantity, _T0),
    )
    repo._conn.commit()


async def _attention(repo: ClerkSqliteRepository) -> list[dict]:
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", _sqlite_repository=repo, account_id=ACCOUNT))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/alpaca/attention")
    assert response.status_code == 200
    return response.json()["items"]


async def test_a_stopped_bot_still_holding_is_one_line_with_flatten_and_finished_bots_are_quiet(
    tmp_path: Path,
) -> None:
    """H13/H34: old flat bots are Finished, never attention; only the bot
    nobody manages is a line, and a running bot manages its own shares."""
    from app.broker.alpaca.clerk.sqlite.commands import submit_start_run

    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT, artifacts_root=tmp_path, clock=_Clock())
    try:
        for sid in ("held", "running", "finished"):
            repo.register_strategy_instance(strategy_instance_id=sid, symbol="SPY", config_hash="h1", config_json=_TRADE_CONFIG)
        submit_start_run(repo, account_id=ACCOUNT, strategy_instance_id="running", lifecycle_run_id="r1", clock=repo.clock)
        _hold_position(repo, "held", 5)
        _hold_position(repo, "running", 1)

        items = await _attention(repo)
    finally:
        repo.close()

    held, legacy = items
    assert held == {
        "condition_id": "stopped-holding:held", "reason_code": "STOPPED_STILL_HOLDING",
        "kind": "stopped_holding", "action": {"label": "Flatten…", "destination": "bot"},
        "severity": "warning", "strategy_instance_id": "held", "symbol": "SPY",
        "headline": "held is stopped but still holds 5 SPY. No bot is managing it.",
        "recovery_status": None,
    }
    # An account not yet on budgets says so once, with Settings as its fix.
    assert (legacy["kind"], legacy["action"], legacy["strategy_instance_id"]) == (
        "legacy_budget", {"label": "Open Settings", "destination": "settings"}, None,
    )
    assert legacy["headline"] == "This account has not switched to budgets. Switch it in Settings to see where its money is."


async def test_holds_channels_and_out_of_sync_orders_each_link_to_where_their_fix_lives(tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.sqlite.uncertainty import raise_account_hold
    from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
        LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
        STREAM_HEALTH_HOLD_REASON_CODE,
        UNEXPLAINED_ORDER_HOLD_REASON_CODE,
    )

    clock = _Clock()
    repo = _budgeted(ClerkSqliteRepository.initialize(account_id=ACCOUNT, artifacts_root=tmp_path, clock=clock))
    try:
        raise_account_hold(repo, reason_code=STREAM_HEALTH_HOLD_REASON_CODE,
                           evidence_refs=["market_data: IBKR connection lost"])
        clock.now_ms += 1
        raise_account_hold(repo, reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, evidence_refs=["loss"], cause_facts={
            "day_start_ms": _T0 - 1, "day_pnl_usd": -100, "loss_limit_usd": 100, "last_equity_usd": 1000,
            "observed_at_ms": _T0,
        })
        clock.now_ms += 1
        raise_account_hold(repo, reason_code=UNEXPLAINED_ORDER_HOLD_REASON_CODE, evidence_refs=["broker-order-1"])

        items = await _attention(repo)
    finally:
        repo.close()

    lines = {item["reason_code"]: (item["kind"], item["action"], item["headline"]) for item in items}
    assert lines == {
        STREAM_HEALTH_HOLD_REASON_CODE: (
            "channel", {"label": "Check connection", "destination": "settings"},
            "New entries are on hold while a connection is down: IBKR market data (IBKR connection lost).",
        ),
        LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE: (
            "hold", {"label": "Open Settings", "destination": "settings"}, "The account is in loss hold",
        ),
        UNEXPLAINED_ORDER_HOLD_REASON_CODE: (
            "out_of_sync", {"label": "Open order records", "destination": "activity"},
            "An order this account did not submit is unreviewed",
        ),
    }
    # Holds are holds: no line calls one "paused".
    assert not any("paused" in item["headline"].lower() for item in items)


async def test_a_bot_whose_exit_has_not_flattened_is_named_once(tmp_path: Path) -> None:
    repo = _budgeted(ClerkSqliteRepository.initialize(account_id=ACCOUNT, artifacts_root=tmp_path, clock=_Clock()))
    try:
        repo.register_strategy_instance(strategy_instance_id="ema-1", symbol="SPY", config_hash="h1", config_json=_TRADE_CONFIG)
        _raise_exit_not_flat(repo, strategy_instance_id="ema-1", symbol="SPY")
        _hold_position(repo, "ema-1", 10)

        items = await _attention(repo)
    finally:
        repo.close()

    assert [(item["kind"], item["strategy_instance_id"]) for item in items] == [("exit", "ema-1")]


def _end_uncleanly(tmp_path: Path, sid: str, *, at_ms: int, kind: str = "EXITED_UNVERIFIED") -> None:
    from app.engine.live.bot_lifecycle_state import (
        BotDutyOutcome,
        BotLifecyclePhase,
        BotLifecycleStateRecord,
        stable_bot_lifecycle_state_path,
    )

    path = stable_bot_lifecycle_state_path(tmp_path, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BotLifecycleStateRecord(
        phase=BotLifecyclePhase.OFF_DUTY, active_run_id=None, last_transition_at_ms=at_ms,
        duty_outcome=BotDutyOutcome(kind=kind, reason_code="EXIT_UNVERIFIED", recorded_at_ms=at_ms, run_id=f"run-{sid}"),
    ).model_dump_json())


def _checked_against_alpaca(repo: ClerkSqliteRepository, *, at_ms: int) -> None:
    repo._conn.execute(
        "INSERT INTO reconciliations (reconciliation_id, effect_operation_id, order_ref, trigger, "
        "attempted_at_ms, outcome, evidence_refs_json) VALUES (?, NULL, NULL, 'AUTOMATIC', ?, 'RESOLVED_SUCCESS', '[]')",
        (f"recon-{at_ms}", at_ms),
    )
    repo._conn.commit()


async def test_flat_bots_that_ended_uncleanly_are_one_account_line_until_the_account_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H13/H34, review A1: seven flat Paper bots that exited unverified were
    seven "could not be verified" lines, because a reconciliation older than
    30 s never vouches. Flat and claiming nothing, each is Finished: the
    account asks once to be checked, and is quiet once it has been."""
    from app.services.broker_v2_panel import sqlite_roster_status

    clock = _Clock()
    repo = _budgeted(ClerkSqliteRepository.initialize(account_id=ACCOUNT, artifacts_root=tmp_path, clock=clock))
    monkeypatch.setattr(sqlite_roster_status, "live_artifacts_root", lambda: tmp_path)
    # A clean check days before, then seven bots end flat but unverified.
    _checked_against_alpaca(repo, at_ms=_T0 - 864_000_000)
    for index in range(7):
        sid = f"paper-{index}"
        repo.register_strategy_instance(strategy_instance_id=sid, symbol="SPY", config_hash="h1", config_json=_TRADE_CONFIG)
        _end_uncleanly(tmp_path, sid, at_ms=_T0 + index)
    facade = SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=object(), trade=object())
    set_active_clerk_runtime(ActiveClerkRuntime(
        authority_kind="sqlite", clerk=facade, _sqlite_repository=repo, account_id=ACCOUNT,
    ))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            before = (await client.get("/api/brokers/alpaca/attention")).json()["items"]
            _checked_against_alpaca(repo, at_ms=_T0 + 60_000)
            after = (await client.get("/api/brokers/alpaca/attention")).json()["items"]
    finally:
        repo.close()

    [line] = before
    assert (line["condition_id"], line["kind"], line["strategy_instance_id"]) == ("positions-unchecked", "out_of_sync", None)
    assert line["action"] == {"label": "Reconcile now", "destination": "reconcile"}
    assert line["headline"].startswith("7 bots ended without a clean exit")
    assert after == []


@pytest.mark.parametrize(
    ("verdict", "cut_before_bot", "intent_unresolved", "line_stays"),
    [
        pytest.param("clean", False, False, False, id="clean-pass-saw-the-bot"),
        pytest.param("position_drift", False, False, True, id="unclean-pass"),
        pytest.param("clean", True, False, True, id="pass-before-the-bots-newest-record"),
        pytest.param("clean", False, True, True, id="clean-pass-with-the-bots-intent-unresolved"),
    ],
)
async def test_the_clerks_own_pass_clears_the_unclean_exit_line_once_it_vouches_for_the_bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    verdict: str, cut_before_bot: bool, intent_unresolved: bool, line_stays: bool,
) -> None:
    """The Clerk compares the account with Alpaca every 15 s, but only a
    Reconcile now ever cleared this line, so it rang until the owner clicked
    a button buried in Activity. A clean pass that saw every record of the
    flat bot is that check; an unclean pass, one older than the bot's newest
    record, or one that still finds an order of the bot's unresolved proves
    nothing about it."""
    from app.broker.alpaca.clerk.sqlite.reconcile import AccountReconciliationResult
    from app.services.broker_v2_panel import sqlite_roster_status

    repo = _budgeted(ClerkSqliteRepository.initialize(account_id=ACCOUNT, artifacts_root=tmp_path, clock=_Clock()))
    monkeypatch.setattr(sqlite_roster_status, "live_artifacts_root", lambda: tmp_path)
    repo.register_strategy_instance(strategy_instance_id="paper-0", symbol="SPY", config_hash="h1", config_json=_TRADE_CONFIG)
    _end_uncleanly(tmp_path, "paper-0", at_ms=_T0)
    facade = SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=object(), trade=object())
    if intent_unresolved:
        monkeypatch.setattr(facade, "_unresolved_order_refs", lambda sid: ("intent:enter:1",))
    newest = repo.last_custody_sequence("paper-0")
    facade.publish_sweep_reconciliation(AccountReconciliationResult(
        verdict=verdict, through_sequence=newest - 1 if cut_before_bot else newest,
    ))
    set_active_clerk_runtime(ActiveClerkRuntime(
        authority_kind="sqlite", clerk=facade, _sqlite_repository=repo, account_id=ACCOUNT,
    ))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            items = (await client.get("/api/brokers/alpaca/attention")).json()["items"]
    finally:
        repo.close()

    assert [item["condition_id"] for item in items] == (["positions-unchecked"] if line_stays else [])


@pytest.mark.parametrize(
    ("verdict", "desk_notices", "last_checked_ms"),
    [
        pytest.param("clean", [], _T0 + 45_000, id="clean-pass"),
        pytest.param(
            "position_drift", ["Position could not be verified"], _T0 - 864_000_000, id="unclean-pass",
        ),
    ],
)
async def test_the_clerks_own_pass_vouches_on_the_desk_and_dates_the_sync_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    verdict: str, desk_notices: list[str], last_checked_ms: int,
) -> None:
    """#2826: the desk's "Position could not be verified" and the status's
    "last checked" counted only a Reconcile now receipt, so a clean 15 s pass
    left the notice up and the time days old. An unclean pass changes neither."""
    from app.broker.alpaca.clerk.sqlite.reconcile import AccountReconciliationResult
    from app.services.broker_v2_panel import sqlite_roster_status

    clock = _Clock()
    repo = _budgeted(ClerkSqliteRepository.initialize(account_id=ACCOUNT, artifacts_root=tmp_path, clock=clock))
    monkeypatch.setattr(sqlite_roster_status, "live_artifacts_root", lambda: tmp_path)
    repo.register_strategy_instance(strategy_instance_id="paper-0", symbol="SPY", config_hash="h1", config_json=_TRADE_CONFIG)
    _end_uncleanly(tmp_path, "paper-0", at_ms=_T0)
    _checked_against_alpaca(repo, at_ms=_T0 - 864_000_000)
    facade = SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=object(), trade=object())
    clock.now_ms = _T0 + 45_000
    facade.publish_sweep_reconciliation(AccountReconciliationResult(
        verdict=verdict, through_sequence=repo.last_custody_sequence("paper-0"),
    ))
    set_active_clerk_runtime(ActiveClerkRuntime(
        authority_kind="sqlite", clerk=facade, _sqlite_repository=repo, account_id=ACCOUNT,
    ))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            desk = await client.get(f"/api/alpaca-clerk-sqlite/accounts/{ACCOUNT}/snapshot")
            status = await client.get("/api/brokers/alpaca/clerk/status")
    finally:
        repo.close()

    assert desk.status_code == status.status_code == 200
    assert [notice["label"] for notice in desk.json()["exposure_notices"]] == desk_notices
    assert status.json()["latest_reconciliation"]["recorded_at_ms"] == last_checked_ms


async def test_a_crashed_bot_whose_run_row_is_still_active_but_holds_shares_is_one_stopped_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review A3: Home files a crashed bot as stopped (its crash outcome),
    so the attention read iterates the same group, not ``active_run``."""
    from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
    from app.services.broker_v2_panel import sqlite_roster_status

    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT, artifacts_root=tmp_path, clock=_Clock())
    monkeypatch.setattr(sqlite_roster_status, "live_artifacts_root", lambda: tmp_path)
    try:
        repo.register_strategy_instance(strategy_instance_id="crashed", symbol="SPY", config_hash="h1", config_json=_TRADE_CONFIG)
        submit_start_run(repo, account_id=ACCOUNT, strategy_instance_id="crashed", lifecycle_run_id="run-crashed", clock=repo.clock)
        _end_uncleanly(tmp_path, "crashed", at_ms=_T0, kind="CRASHED")
        _hold_position(repo, "crashed", 1)

        items = await _attention(repo)
    finally:
        repo.close()

    assert [
        (item["kind"], item["strategy_instance_id"], item["action"]["label"])
        for item in items if item["kind"] != "legacy_budget"
    ] == [("stopped_holding", "crashed", "Flatten…")]


# ── The account's own standing (review B5) ──────────────────────────────────


async def test_a_failed_custody_authority_is_a_line_not_a_quiet_home() -> None:
    from app.broker.alpaca.clerk.active_runtime import ClerkStartupFailure

    set_active_clerk_runtime(ActiveClerkRuntime(
        authority_kind="unavailable", account_id=ACCOUNT,
        startup_failure=ClerkStartupFailure(
            reason_code="AUTHORITY_CHAIN_BROKEN", account_id=ACCOUNT, scope="ACCOUNT_CLERK",
            impact="No order can be placed.", recovery="Run the recovery CLI.", observed_at_ms=_T0,
            activation_detected=True,
        ),
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/alpaca/attention")

    body = response.json()
    assert body["account_id"] == ACCOUNT
    [line] = body["items"]
    # The lane holds no repository, so order records answer 503; Settings
    # loads and names the refusal's recovery (#2620).
    assert (line["kind"], line["severity"], line["headline"], line["action"]["destination"]) == (
        "account", "blocking", "This account's custody authority has failed.", "settings",
    )


async def test_an_authority_reconnecting_to_alpaca_is_a_line_that_says_so_not_a_failure() -> None:
    """#2582: a startup that could not reach Alpaca has not failed, and its line says so."""
    from app.broker.alpaca.clerk.active_runtime import reconnecting_refusal
    from app.broker.contract.errors import BrokerUnreachable

    set_active_clerk_runtime(reconnecting_refusal(
        BrokerUnreachable("Could not reach Alpaca while fetching positions.", broker="alpaca"),
        account_id=ACCOUNT, activation_detected=True,
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/alpaca/attention")

    [line] = response.json()["items"]
    assert (line["condition_id"], line["severity"]) == ("account:authority-reconnecting", "warning")
    assert line["headline"] == (
        "This account's Clerk could not reach Alpaca when it started. "
        "It is reconnecting and will recover on its own."
    )


async def test_an_account_alpaca_blocked_is_a_line_from_the_cached_observation_without_a_broker_read(
    tmp_path: Path,
) -> None:
    from app.broker.contract.registry import get_broker_registry, reset_broker_registry_for_testing
    from app.services import broker_account_snapshot
    from tests.broker.v2panel.conftest import account_snapshot

    class _NoReads:
        broker_id = "alpaca"

        async def get_account(self) -> None:  # pragma: no cover - the read must not happen
            raise AssertionError("the attention read never contacts the broker")

    reset_broker_registry_for_testing()
    port = _NoReads()
    get_broker_registry().register(port)  # type: ignore[arg-type]
    broker_account_snapshot._snapshot_cache["alpaca"] = (
        port, account_snapshot(account_id=ACCOUNT, trading_blocked=True), _T0 * 10,  # type: ignore[arg-type]
    )
    repo = _budgeted(ClerkSqliteRepository.initialize(account_id=ACCOUNT, artifacts_root=tmp_path, clock=_Clock()))
    facade = SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=object(), trade=object())
    set_active_clerk_runtime(ActiveClerkRuntime(
        authority_kind="sqlite", clerk=facade, _sqlite_repository=repo, account_id=ACCOUNT,
    ))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            blocked = (await client.get("/api/brokers/alpaca/attention")).json()["items"]
            broker_account_snapshot._snapshot_cache.clear()
            unobserved = (await client.get("/api/brokers/alpaca/attention")).json()["items"]
    finally:
        broker_account_snapshot._snapshot_cache.clear()
        reset_broker_registry_for_testing()
        repo.close()

    [line] = blocked
    assert (line["kind"], line["reason_code"], line["headline"], line["action"]) == (
        "account", "alpaca_account_trading_blocked", "Alpaca has blocked trading on this account",
        {"label": "Open Settings", "destination": "settings"},
    )
    # Nothing observed is nothing claimed either way.
    assert unobserved == []
