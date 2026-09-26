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
from app.broker.alpaca.clerk.sqlite.exit_recovery import observe_exit_recovery
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
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT, artifacts_root=tmp_path, clock=clock
    )
    for strategy_instance_id, symbol in (("ema-1", "SPY"), ("ema-2", "QQQ")):
        repo.register_strategy_instance(
            strategy_instance_id=strategy_instance_id, symbol=symbol, config_hash="h1"
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
    assert first["kind"] == "uncertainty"
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
    repo.register_strategy_instance(strategy_instance_id="ema-3", symbol="IWM", config_hash="h1")
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
            next_attempt_at_ms=next_attempt_at_ms,
        )
        episode = repo.active_uncertainty(
            scope="CUSTODY_SUBJECT", reason_code=EXIT_NOT_FLAT_REASON_CODE,
            strategy_instance_id=strategy_instance_id,
        )
        observe_exit_recovery(
            repo, strategy_instance_id=strategy_instance_id, uncertainty_id=episode["uncertainty_id"],
            outcome="hold", reason_code="NO_SESSION_OPEN", allowed_from_ms=next_attempt_at_ms,
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
    by_sid = {item["strategy_instance_id"]: item for item in response.json()["items"]}
    assert by_sid["ema-1"]["next_attempt_at_ms"] == wednesday_after_hours + 3_600_000
    assert by_sid["ema-2"]["recovery_status"]["kind"] == "allowed_now"
    assert by_sid["ema-2"]["next_attempt_at_ms"] is None
    unreadable = by_sid["ema-3"]
    assert (
        unreadable["symbol"],
        unreadable["next_attempt_at_ms"],
        unreadable["facts_unreadable"],
        unreadable["exit_working"],
    ) == (None, None, True, False)
    overnight_ema_2 = next(
        item for item in overnight.json()["items"] if item["strategy_instance_id"] == "ema-2"
    )
    assert overnight_ema_2["next_attempt_at_ms"] is None
    assert overnight_ema_2["recovery_status"]["kind"] == "unknown"
    assert overnight_ema_2["recovery_status"]["last_checked_at_ms"] == by_sid["ema-2"]["recovery_status"]["last_checked_at_ms"]
    assert "next_attempt_overdue" not in overnight_ema_2


@pytest.mark.asyncio
async def test_attention_without_facade_retains_notice_but_cannot_invent_retry_time(tmp_path: Path) -> None:
    clock = _Clock()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT, artifacts_root=tmp_path, clock=clock)
    repo.register_strategy_instance(strategy_instance_id="ema-1", symbol="SPY", config_hash="h1")
    raise_uncertainty(
        repo, strategy_instance_id="ema-1", reason_code=EXIT_NOT_FLAT_REASON_CODE,
        headline="Exit is not flat", explanation="Position remains", operator_impact="Entry blocked",
        next_step="Reconcile", cause_facts={"symbol": "SPY"}, severity="blocking",
        next_attempt_at_ms=clock.now_ms + 60_000,
    )
    set_active_clerk_runtime(ActiveClerkRuntime(
        authority_kind="sqlite", _sqlite_repository=repo, account_id=ACCOUNT,
    ))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/brokers/alpaca/attention")
        item, = response.json()["items"]
        assert item["symbol"] == "SPY"
        assert item["next_attempt_at_ms"] is None
        assert "next_attempt_overdue" not in item
    finally:
        repo.close()


async def test_dead_run_notice_reaches_account_desk_and_bell_without_selling(
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
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT, artifacts_root=tmp_path, clock=clock)
    repo.register_strategy_instance(strategy_instance_id="dead-bot", symbol="SPY", config_hash="h1")
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
        [notice] = bell.json()["items"]
        assert notice["headline"] == "Position could not be verified; check the broker"
        assert notice["strategy_instance_id"] == "dead-bot"
        assert desk.status_code == 200
        assert desk.json()["exposure_notices"][0]["label"] == notice["headline"]
    finally:
        repo.close()
