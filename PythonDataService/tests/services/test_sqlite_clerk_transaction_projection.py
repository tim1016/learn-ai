"""Account Desk adaptation over active SQLite materialized folds."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.models import BrokerOrderLeg
from app.services.sqlite_clerk_transaction_projection import (
    sqlite_transaction_detail,
    sqlite_transaction_history,
)


class _UnusedBroker:
    broker_id = "alpaca"

    async def list_orders(self, **_kwargs: Any) -> list:
        return []

    async def list_positions(self) -> list:
        return []


def test_account_desk_reads_operation_first_sqlite_projection(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-ACCOUNT-DESK",
        artifacts_root=tmp_path,
    )
    repo.register_strategy_instance(
        strategy_instance_id="spy-bot",
        symbol="SPY",
        config_hash="config-1",
    )
    submit_start_run(
        repo,
        account_id="PA-ACCOUNT-DESK",
        strategy_instance_id="spy-bot",
        lifecycle_run_id="run-1",
    )
    accepted = accept_enter(
        repo,
        account_id="PA-ACCOUNT-DESK",
        strategy_instance_id="spy-bot",
        decision_id="decision-1",
        lifecycle_run_id="run-1",
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
    )
    broker = _UnusedBroker()
    set_active_clerk_runtime(
        ActiveClerkRuntime(
            authority_kind="sqlite",
            clerk=SqliteAlpacaClerkFacade(
                repo=repo,
                read=broker,  # type: ignore[arg-type]
                trade=broker,  # type: ignore[arg-type]
            ),
        )
    )
    try:
        page = sqlite_transaction_history(
            account_id="PA-ACCOUNT-DESK",
            limit=25,
            cursor=None,
            origin=None,
            lifecycle_state=None,
            strategy_instance_id=None,
            run_id=None,
        )
        assert page is not None
        active, detail = sqlite_transaction_detail(
            account_id="PA-ACCOUNT-DESK",
            transaction_id=accepted.effect_operation_id or "",
        )
    finally:
        set_active_clerk_runtime(None)
        repo.close()

    assert page.feed_headline == "SQLite custody projection live"
    assert page.canonical_fallback_required is False
    assert len(page.rows) == 1
    assert page.rows[0].transaction_id == accepted.effect_operation_id
    assert page.rows[0].event_count == 1
    assert active is True
    assert detail is not None
    assert detail.events[0].receipt["clerk_observed_at_ms"]
    assert detail.events[0].receipt["recorded_at_ms"]
    assert "source_event_at_ms" in detail.events[0].receipt
