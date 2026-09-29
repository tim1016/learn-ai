"""One account's bot history, as its Clerk serves it (#2574).

The account's own database and each Dry Run's own ``sim:`` database are
read and worded here: status by the catalog's rules, the end of each run in
plain words from its durable receipt, every dollar authored in Python. A
Dry Run whose records cannot be read is a named gap beside every row that
could be -- never a missing row.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy, append_risk_policy
from app.broker.alpaca.clerk.sqlite.budget_authority import commit_budget_authority_cutover
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.services.bot_binding_repository import (
    BotRunOutcomeRecord,
    BotRunRecord,
    live_state_binding_repository,
)
from app.services.broker_v2_panel import bot_history, sqlite_roster_status
from app.services.broker_v2_panel.bot_history import outcome_headline
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock
from tests.broker.alpaca.clerk.sqlite.test_bot_history import _ack, _enter
from tests.broker.alpaca.clerk.sqlite.test_budget_claims import _record_sale
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS, _deploy, _gate
from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_slice

_ACCOUNT = "BUDGET-PAPER"


def _register(repo: ClerkSqliteRepository, sid: str, *, mode: str = "trade") -> None:
    repo.register_strategy_instance(
        strategy_instance_id=sid, symbol="SPY", config_hash=f"seal-{sid}", exit_terms=TERMS,
        strategy_key="ema_crossover", display_name="EMA crossover",
        config_json=json.dumps({"mode": mode, "carryover_policy": "FORBID", "quantity": 1}),
    )


def _receipt(root: Path, sid: str, run: str, kind: str, reason: str) -> None:
    """The run's launch evidence, then its create-once terminal receipt."""
    runs = root / "live_state" / sid / "runs"
    runs.mkdir(parents=True)
    (runs / f"{run}.json").write_text(BotRunRecord(
        run_id=run, strategy_instance_id=sid, configuration_hash="0" * 64, launch_reason="deploy",
        started_at_ms=NOON,
    ).model_dump_json(), encoding="utf-8")
    live_state_binding_repository(root).record_outcome(BotRunOutcomeRecord(
        strategy_instance_id=sid, run_id=run, kind=kind, reason_code=reason, recorded_at_ms=NOON + 1,  # type: ignore[arg-type]
    ))


def _paper_account(directory: Path) -> ClerkSqliteRepository:
    """``done`` bought 1 SPY at 100, sold it at 110 and was stopped; ``live`` still runs."""
    repo = ClerkSqliteRepository.initialize(account_id=_ACCOUNT, artifacts_root=directory, clock=_TestClock(NOON))
    commit_budget_authority_cutover(repo, actor="owner", reviewed_token="empty-account", stop_receipt="no-runs")
    append_risk_policy(repo, policy=AccountRiskPolicy(1, .1, 100, "profile", 1, "owner", NOON), expected_revision=0)
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
    for sid in ("done", "live"):
        _register(repo, sid)
        _deploy(repo, sid, 15_000)
    traded = _enter(repo, "done", "run-done", "enter-done", envelope=_gate())
    _ack(repo, traded, "filled")
    _append_slice(repo, traded, execution_id="done-buy", quantity=1, source_event_at_ms=NOON - 2, fee=0)
    _record_sale(repo, traded, key="done-sell", price=110, at_ms=NOON - 1)
    submit_stop_run(repo, account_id=repo.account_id, strategy_instance_id="done", lifecycle_run_id="run-done", clock=repo.clock)
    return repo


def _dry_run(root: Path) -> None:
    """Dry Run ``dry-1``: one run in its own simulated account, ended at the close."""
    dry = ClerkSqliteRepository.initialize(account_id="sim:dry-1", artifacts_root=root, clock=_TestClock(NOON))
    try:
        _register(dry, "dry-1", mode="dry_run")
        submit_start_run(dry, account_id=dry.account_id, strategy_instance_id="dry-1", lifecycle_run_id="run-dry", clock=dry.clock)
        submit_stop_run(dry, account_id=dry.account_id, strategy_instance_id="dry-1", lifecycle_run_id="run-dry", clock=dry.clock)
    finally:
        dry.close()


@pytest.fixture
def lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A Paper lane: its account, Dry Run ``dry-1``, and Dry Run ``dry-lost`` with no records."""
    root = tmp_path / "artifacts"
    main = _paper_account(tmp_path / "main")
    _dry_run(root)
    _receipt(root, "done", "run-done", "STOPPED", "STOPPED_FLAT")
    _receipt(root, "dry-1", "run-dry", "CLOCKED_OUT_FLAT", "SESSION_CLOSED")
    facade = SimpleNamespace(account_id=_ACCOUNT, account_mode="paper", repository=main)
    registry = SimpleNamespace(bindings_for_broker=lambda _broker: [
        SimpleNamespace(strategy_instance_id="dry-1", mode="dry_run"),
        SimpleNamespace(strategy_instance_id="dry-lost", mode="dry_run"),
        SimpleNamespace(strategy_instance_id="done", mode="trade"),
    ])

    async def accept(_broker: str, account_id: str) -> str:
        return account_id

    monkeypatch.setattr(bot_history, "validate_account", accept)
    monkeypatch.setattr(bot_history, "active_sqlite_facade", lambda _broker: facade)
    monkeypatch.setattr(bot_history, "custody_account_id_for_route", lambda _broker, resolved: resolved)
    monkeypatch.setattr(bot_history, "get_bot_task_registry", lambda: registry)
    monkeypatch.setattr(bot_history, "live_artifacts_root", lambda: root)
    monkeypatch.setattr(sqlite_roster_status, "live_artifacts_root", lambda: root)
    yield main
    main.close()


@pytest.mark.asyncio
async def test_every_bot_is_listed_with_its_world_status_outcome_and_money(lane: ClerkSqliteRepository) -> None:
    history = await bot_history.account_bot_history("alpaca", _ACCOUNT)

    bots = {bot.strategy_instance_id: bot for bot in history.bots}
    assert set(bots) >= {"done", "live", "dry-1"}
    done = bots["done"]
    assert (done.world, done.world_label) == ("paper", "PAPER · practice money")
    assert (done.status, done.status_label) == ("finished", "Finished")
    assert done.outcome is not None and done.outcome.headline == "Stopped by you"
    assert done.transaction_count == 2 and done.orders.filled == 1
    assert (done.budget_usd, done.result_usd, done.fees_usd) == ("150.00", "10.00", "0.00")
    assert done.money_unavailable_reason is None and done.money_scope_note is None
    assert done.started_at_ms is not None and done.stopped_at_ms is not None
    assert bots["live"].status == "running" and bots["live"].outcome is None
    dry = bots["dry-1"]
    assert (dry.world, dry.world_label) == ("dry_run", "DRY RUN · simulated cash")
    assert dry.outcome is not None and dry.outcome.headline == "Finished its day flat"
    assert dry.account_id == _ACCOUNT


@pytest.mark.asyncio
async def test_an_unreadable_dry_run_is_a_named_gap_never_a_missing_row(lane: ClerkSqliteRepository) -> None:
    history = await bot_history.account_bot_history("alpaca", _ACCOUNT)

    assert [gap.strategy_instance_id for gap in history.gaps] == ["dry-lost"]
    assert "could not be read" in history.gaps[0].reason
    assert "dry-1" in {bot.strategy_instance_id for bot in history.bots}


@pytest.mark.parametrize(
    ("kind", "reason", "flattened", "headline"),
    [
        ("CLOCKED_OUT_FLAT", "SESSION_CLOSED", False, "Finished its day flat"),
        ("STOPPED", "STOPPED_FLAT", False, "Stopped by you"),
        ("STOPPED", "STOPPED_FLAT", True, "Stopped and flattened"),
        ("STOPPED", "SERVICE_SHUTDOWN", False, "Stopped when the service shut down"),
        ("CRASHED", "FEED_DEATH", False, "Crashed because market data stopped"),
        ("CRASHED", "ValueError", False, "Crashed"),
        ("HALTED", "HALTED", False, "Halted"),
        ("FAILED_LAUNCH", "LAUNCH_FAILED", False, "Failed to launch"),
        ("EXITED_UNVERIFIED", "INTERRUPTED_BY_RESTART", False, "Ended without a clean exit"),
    ],
)
def test_each_way_a_run_ends_has_its_own_plain_words(kind: str, reason: str, flattened: bool, headline: str) -> None:
    assert outcome_headline(kind, reason, flattened=flattened) == headline


@pytest.mark.asyncio
async def test_the_route_serves_the_read_and_refuses_without_the_accounts_clerk(
    lane: ClerkSqliteRepository, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.routers.broker_v2_panel import router

    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        served = await client.get(f"/api/brokers/alpaca/accounts/{_ACCOUNT}/bot-history")
        monkeypatch.setattr(bot_history, "active_sqlite_facade", lambda _broker: None)
        refused = await client.get(f"/api/brokers/alpaca/accounts/{_ACCOUNT}/bot-history")

    assert served.status_code == 200
    assert {bot["strategy_instance_id"] for bot in served.json()["bots"]} >= {"done", "live", "dry-1"}
    assert refused.status_code == 503
