"""Account Desk adaptation over active SQLite materialized folds."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.economic_projection import ExecutionRow
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
from app.broker.alpaca.clerk.sqlite.facts import ExecutionSliceFilledFacts
from app.broker.alpaca.clerk.sqlite.manual_orders import accept_manual_order
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.projection_models import (
    OperationPage,
    ProjectedCommand,
    ProjectedOperation,
    ProjectedOrder,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg, OrderSide
from app.engine.live.order_identity import build_manual_order_namespace, build_order_ref
from app.services.clerk_transaction_projection import ClerkTransactionProjectionUnavailable
from app.services.sqlite_clerk_transaction_projection import (
    _assert_history_hydration_revision,
    _detail_row,
    _origin_filtered_operation_page,
    _summary_row,
    classify_transaction_origin,
    sqlite_transaction_detail,
    sqlite_transaction_history,
)


class _UnusedBroker:
    broker_id = "alpaca"

    async def list_orders(self, **_kwargs: Any) -> list:
        return []

    async def list_positions(self) -> list:
        return []


def _append_execution(
    repo: ClerkSqliteRepository,
    *,
    order_ref: str,
    command_id: str,
    effect_operation_id: str,
    run_id: str,
    execution_id: str,
    quantity: float,
    price: float,
    fee: float | None,
    fee_fidelity: str,
) -> None:
    facts = ExecutionSliceFilledFacts(
        execution_id=execution_id,
        symbol="SPY",
        side="BUY",
        slice_qty=quantity,
        slice_price=price,
        fee=fee,
        fee_fidelity=fee_fidelity,
        evidence_source="websocket",
        source_event_at_ms=1_786_370_000_000,
    )
    result = repo.append_execution_slice_if_absent(
        execution_id=execution_id,
        order_ref=order_ref,
        build_transition=lambda: TransitionInput(
            strategy_instance_id="spy-bot",
            run_id=run_id,
            command_id=command_id,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            transition_kind="EXECUTION_SLICE_FILLED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            source_event_at_ms=facts.source_event_at_ms,
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXECUTION_SLICE_FILLED",
            facts_json=facts.to_facts_json(),
        ),
        build_coverage_conflict=lambda: (_ for _ in ()).throw(
            AssertionError("fixture must not use recovery evidence")
        ),
    )
    assert result == "appended"


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
        recorded_at_ms = page.rows[0].recorded_at_ms
        in_window_page = sqlite_transaction_history(
            account_id="PA-ACCOUNT-DESK",
            limit=25,
            cursor=None,
            origin=None,
            lifecycle_state=None,
            strategy_instance_id=None,
            run_id=None,
            from_ms=recorded_at_ms,
            to_ms=recorded_at_ms,
        )
        outside_window_page = sqlite_transaction_history(
            account_id="PA-ACCOUNT-DESK",
            limit=25,
            cursor=None,
            origin=None,
            lifecycle_state=None,
            strategy_instance_id=None,
            run_id=None,
            from_ms=recorded_at_ms + 1,
            to_ms=recorded_at_ms + 2,
        )
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
    assert in_window_page is not None
    assert [row.transaction_id for row in in_window_page.rows] == [accepted.effect_operation_id]
    assert outside_window_page is not None
    assert outside_window_page.rows == []
    assert active is True
    assert detail is not None
    assert detail.events[0].receipt["clerk_observed_at_ms"]
    assert detail.events[0].receipt["recorded_at_ms"]
    assert "source_event_at_ms" in detail.events[0].receipt


def test_account_transaction_projection_includes_manual_ticket_without_bot_run(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-ACCOUNT-DESK",
        artifacts_root=tmp_path,
    )
    accepted = accept_manual_order(
        repo,
        account_id="PA-ACCOUNT-DESK",
        operator_id="operator-42",
        ticket_id="7de3a77c-b698-4e0d-a5d1-2f624574ed35",
        leg_id="09d6d63e-6375-4e6d-8d20-3b1bf70c2465",
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
            origin="manual",
            lifecycle_state=None,
            strategy_instance_id=None,
            run_id=None,
        )
        active, detail = sqlite_transaction_detail(
            account_id="PA-ACCOUNT-DESK",
            transaction_id=accepted.leg.effect_operation_id or "",
        )
    finally:
        set_active_clerk_runtime(None)
        repo.close()

    assert page is not None
    assert len(page.rows) == 1
    summary = page.rows[0]
    assert summary.transaction_origin == "manual"
    assert summary.subject_id == "manual-operator:operator-42"
    assert summary.strategy_instance_id is None
    assert summary.run_id is None
    assert summary.order_ref == accepted.leg.order_ref
    assert active is True
    assert detail is not None
    assert detail.subject_id == "manual-operator:operator-42"
    assert detail.receipt["subject_id"] == "manual-operator:operator-42"
    assert [event.event_kind for event in detail.events] == ["MANUAL_ORDER_ACCEPTED"]


def test_transaction_projection_preserves_each_effective_execution_economics(
    tmp_path: Path,
) -> None:
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
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=3),
    )
    assert accepted.effect_operation_id is not None
    assert accepted.order_ref is not None
    _append_execution(
        repo,
        order_ref=accepted.order_ref,
        command_id=accepted.command.command_id,
        effect_operation_id=accepted.effect_operation_id,
        run_id=accepted.command.run_id,
        execution_id="execution-reported",
        quantity=1.0,
        price=501.25,
        fee=0.12,
        fee_fidelity="reported",
    )
    _append_execution(
        repo,
        order_ref=accepted.order_ref,
        command_id=accepted.command.command_id,
        effect_operation_id=accepted.effect_operation_id,
        run_id=accepted.command.run_id,
        execution_id="execution-fee-pending",
        quantity=2.0,
        price=502.5,
        fee=None,
        fee_fidelity="not_reported",
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
            origin="strategy",
            lifecycle_state=None,
            strategy_instance_id=None,
            run_id=None,
        )
        active, detail = sqlite_transaction_detail(
            account_id="PA-ACCOUNT-DESK",
            transaction_id=accepted.effect_operation_id,
        )
    finally:
        set_active_clerk_runtime(None)
        repo.close()

    assert page is not None
    assert page.rows[0].transaction_origin == "strategy"
    assert page.rows[0].execution_quantity is None
    assert page.rows[0].execution_price is None
    assert page.rows[0].fee_fidelity is None
    assert active is True
    assert detail is not None
    economics = [event for event in detail.events if event.event_kind == "execution_fill"]
    assert {
        event.native_execution_id: (
            event.execution_quantity,
            event.execution_price,
            event.fee,
            event.fee_fidelity,
        )
        for event in economics
    } == {
        "execution-reported": (1.0, 501.25, 0.12, "reported"),
        "execution-fee-pending": (2.0, 502.5, None, "not_reported"),
    }


def test_transaction_projection_surfaces_single_slice_economics_on_summary(tmp_path: Path) -> None:
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
    assert accepted.effect_operation_id is not None
    assert accepted.order_ref is not None
    _append_execution(
        repo,
        order_ref=accepted.order_ref,
        command_id=accepted.command.command_id,
        effect_operation_id=accepted.effect_operation_id,
        run_id=accepted.command.run_id,
        execution_id="execution-only",
        quantity=1.0,
        price=501.25,
        fee=0.12,
        fee_fidelity="reported",
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
            origin="strategy",
            lifecycle_state=None,
            strategy_instance_id=None,
            run_id=None,
        )
    finally:
        set_active_clerk_runtime(None)
        repo.close()

    assert page is not None
    row = page.rows[0]
    assert (
        row.native_execution_id,
        row.execution_quantity,
        row.execution_price,
        row.fee,
        row.fee_fidelity,
    ) == ("execution-only", 1.0, 501.25, 0.12, "reported")


def test_exit_projection_uses_only_reducing_order_execution_economics() -> None:
    """Linked entry custody must never leak entry fills into an EXIT receipt."""
    entry = _projected_operation(
        effect_operation_id="exit-1",
        strategy_instance_id="spy-bot",
        order_ref="learn-ai/spy-bot/v1:entry",
        created_at_ms=1_000,
    )
    reducing = ProjectedOrder(
        order_ref="learn-ai/spy-bot/v1:exit",
        client_order_id="learn-ai/spy-bot/v1:exit",
        broker_order_id="broker-exit",
        role="REDUCING",
        broker_state="filled",
        submitted_at_ms=1_001,
        updated_at_ms=1_002,
        symbol="SPY",
    )
    operation = replace(entry, kind="EXIT", orders=(*entry.orders, reducing))
    entry_execution = ExecutionRow(
        fill_id="fill-entry",
        execution_id="exec-entry",
        order_ref=entry.orders[0].order_ref,
        strategy_instance_id="spy-bot",
        origin="strategy",
        state="effective",
        event_kind="fill",
        symbol="SPY",
        side=OrderSide.BUY,
        quantity=1.0,
        price=500.0,
        fee=None,
        fee_fidelity="not_reported",
        filled_at_ms=1_000,
        recorded_at_ms=1_000,
        journal_seq=4,
    )
    reducing_execution = replace(
        entry_execution,
        fill_id="fill-exit",
        execution_id="exec-exit",
        order_ref=reducing.order_ref,
        side=OrderSide.SELL,
        price=501.0,
        journal_seq=7,
    )

    summary = _summary_row(
        operation,
        account_id="PA-ACCOUNT-DESK",
        execution_summaries={
            entry.orders[0].order_ref: (entry_execution,),
            reducing.order_ref: (reducing_execution,),
        },
    )
    detail = _detail_row(
        operation,
        (),
        account_id="PA-ACCOUNT-DESK",
        executions=(entry_execution, reducing_execution),
    )

    assert summary.order_ref == reducing.order_ref
    assert summary.native_execution_id == "exec-exit"
    assert summary.execution_price == 501.0
    execution_events = [event for event in detail.events if event.event_kind == "execution_fill"]
    assert [(event.native_execution_id, event.journal_seq) for event in execution_events] == [
        ("exec-exit", 7)
    ]


def test_transaction_origin_uses_exact_ownership_ladder_without_namespace_prefix_inference() -> None:
    strategy_ref = "learn-ai/spy-bot/v1:intent-1"
    manual_ref = build_order_ref(build_manual_order_namespace("operator"), "intent-2")

    assert classify_transaction_origin(
        order_ref=strategy_ref,
        strategy_instance_id="spy-bot",
    ) == "strategy"
    assert classify_transaction_origin(
        order_ref=manual_ref,
        strategy_instance_id=None,
        custody_subject_kind="MANUAL_OPERATOR",
    ) == "manual"
    assert classify_transaction_origin(
        order_ref=manual_ref,
        strategy_instance_id=None,
    ) == "unknown"
    assert classify_transaction_origin(
        order_ref="learn-ai/spy-bot/v10:intent-3",
        strategy_instance_id="spy-bot",
    ) == "unknown"
    assert classify_transaction_origin(
        order_ref="foreign-broker-ref",
        strategy_instance_id=None,
        external_observation=True,
    ) == "external"


def test_external_transaction_history_and_detail_stay_outside_bot_economics(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-ACCOUNT-DESK",
        artifacts_root=tmp_path,
    )
    observed = observe_external_order(
        repo,
        order=BrokerOrder(
            broker="alpaca",
            order_id="external-order-1",
            client_order_id="alpaca-console:operator-order-1",
            symbol="SPY",
            asset_class="us_equity",
            side="BUY",
            order_type="market",
            time_in_force="day",
            quantity=2.0,
            filled_quantity=0.0,
            limit_price=None,
            stop_price=None,
            filled_avg_price=None,
            status="accepted",
            submitted_at_ms=None,
            created_at_ms=None,
            updated_at_ms=None,
            filled_at_ms=None,
            canceled_at_ms=None,
            expired_at_ms=None,
            observed_at_ms=1_786_370_000_000,
        ),
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
            origin="external",
            lifecycle_state=None,
            strategy_instance_id=None,
            run_id=None,
        )
        active, detail = sqlite_transaction_detail(
            account_id="PA-ACCOUNT-DESK",
            transaction_id="external-order:external-order-1",
        )
    finally:
        set_active_clerk_runtime(None)
        repo.close()

    assert page is not None
    assert len(page.rows) == 1
    row = page.rows[0]
    assert row.transaction_id == "external-order:external-order-1"
    assert row.transaction_origin == "external"
    assert row.strategy_instance_id is None
    assert row.order_ref is None
    assert row.order_instruction.symbol == "SPY"
    assert row.order_instruction.quantity == 2.0
    assert row.journal_seq == observed.observation_sequence
    assert row.recorded_at_ms == observed.observation_recorded_at_ms
    assert row.execution_quantity is None
    assert row.execution_price is None
    assert row.fee is None
    assert row.fee_fidelity is None
    assert active is True
    assert detail is not None
    assert [event.event_kind for event in detail.events] == ["external_order_observed"]
    assert detail.events[0].journal_seq == observed.observation_sequence
    assert detail.events[0].recorded_at_ms == observed.observation_recorded_at_ms


def test_external_history_applies_window_before_keyset_limit(tmp_path: Path) -> None:
    now_ms = [1_000]
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-ACCOUNT-DESK",
        artifacts_root=tmp_path,
        clock=lambda: now_ms[0],
    )

    def order(order_id: str, observed_at_ms: int) -> BrokerOrder:
        return BrokerOrder(
            broker="alpaca",
            order_id=order_id,
            client_order_id=f"alpaca-console:{order_id}",
            symbol="SPY",
            asset_class="us_equity",
            side="BUY",
            order_type="market",
            time_in_force="day",
            quantity=1.0,
            filled_quantity=0.0,
            limit_price=None,
            stop_price=None,
            filled_avg_price=None,
            status="accepted",
            submitted_at_ms=None,
            created_at_ms=None,
            updated_at_ms=None,
            filled_at_ms=None,
            canceled_at_ms=None,
            expired_at_ms=None,
            observed_at_ms=observed_at_ms,
        )

    observe_external_order(repo, order=order("older-in-window", 1_000))
    now_ms[0] = 2_000
    observe_external_order(repo, order=order("newer-outside-window", 2_000))
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
            limit=1,
            cursor=None,
            origin="external",
            lifecycle_state=None,
            strategy_instance_id=None,
            run_id=None,
            from_ms=1_000,
            to_ms=1_500,
        )
    finally:
        set_active_clerk_runtime(None)
        repo.close()

    assert page is not None
    assert [row.transaction_id for row in page.rows] == [
        "external-order:older-in-window"
    ]
    assert page.next_cursor is None


def test_all_transaction_history_keyset_merges_strategy_and_external_without_duplicates(
    tmp_path: Path,
) -> None:
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
    observe_external_order(
        repo,
        order=BrokerOrder(
            broker="alpaca",
            order_id="external-order-1",
            client_order_id="alpaca-console:operator-order-1",
            symbol="AAPL",
            asset_class="us_equity",
            side="SELL",
            order_type="market",
            time_in_force="day",
            quantity=2.0,
            filled_quantity=0.0,
            limit_price=None,
            stop_price=None,
            filled_avg_price=None,
            status="accepted",
            submitted_at_ms=None,
            created_at_ms=None,
            updated_at_ms=None,
            filled_at_ms=None,
            canceled_at_ms=None,
            expired_at_ms=None,
            observed_at_ms=1_786_370_000_000,
        ),
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
        cursor: str | None = None
        rows = []
        for _ in range(10):
            page = sqlite_transaction_history(
                account_id="PA-ACCOUNT-DESK",
                limit=1,
                cursor=cursor,
                origin=None,
                lifecycle_state=None,
                strategy_instance_id=None,
                run_id=None,
            )
            assert page is not None
            rows.extend(page.rows)
            cursor = page.next_cursor
            if cursor is None:
                break
        else:
            raise AssertionError("all-history keyset page did not terminate")
    finally:
        set_active_clerk_runtime(None)
        repo.close()

    transaction_ids = [row.transaction_id for row in rows]
    assert len(transaction_ids) == len(set(transaction_ids))
    assert accepted.effect_operation_id in transaction_ids
    external = next(
        row for row in rows if row.transaction_id == "external-order:external-order-1"
    )
    assert external.transaction_origin == "external"
    assert external.strategy_instance_id is None
    assert external.execution_quantity is None


def test_all_history_refuses_typed_hydration_that_crosses_a_control_revision(
    tmp_path: Path,
) -> None:
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-ACCOUNT-DESK",
        artifacts_root=tmp_path,
    )
    try:
        before = repo.control_meta_snapshot()
        after = replace(before, control_revision=before.control_revision + 1)

        with pytest.raises(ClerkTransactionProjectionUnavailable, match="changed during"):
            _assert_history_hydration_revision(before=before, after=after)
    finally:
        repo.close()


def _projected_operation(
    *,
    effect_operation_id: str,
    strategy_instance_id: str | None,
    order_ref: str,
    created_at_ms: int,
    subject_id: str | None = None,
    custody_subject_kind: str | None = None,
) -> ProjectedOperation:
    run_id = "run-1" if strategy_instance_id is not None else None
    return ProjectedOperation(
        effect_operation_id=effect_operation_id,
        kind="enter",
        state="succeeded",
        custody_owner="ACCOUNT_CLERK",
        strategy_instance_id=strategy_instance_id,
        run_id=run_id,
        created_at_ms=created_at_ms,
        updated_at_ms=created_at_ms,
        latest_transition_sequence=1,
        transition_count=1,
        terminal_receipt_id=None,
        command=ProjectedCommand(
            command_id=f"{effect_operation_id}-command",
            kind="enter",
            action="submit",
            state="succeeded",
            run_id=run_id,
            receipt_id=None,
            created_at_ms=created_at_ms,
            updated_at_ms=created_at_ms,
        ),
        orders=(
            ProjectedOrder(
                order_ref=order_ref,
                client_order_id=order_ref,
                broker_order_id=None,
                role="entry",
                broker_state=None,
                submitted_at_ms=created_at_ms,
                updated_at_ms=created_at_ms,
                symbol="SPY",
            ),
        ),
        subject_id=subject_id,
        custody_subject_kind=custody_subject_kind,
    )


class _StubReader:
    """Hands out one raw page per call, newest-first, like the real reader."""

    def __init__(self, pages: list[OperationPage]) -> None:
        self._pages = list(pages)
        self.calls = 0

    def operation_page(self, **_kwargs: Any) -> OperationPage:
        page = self._pages[self.calls]
        self.calls += 1
        return page


class _StubEconomicReader:
    def effective_execution_summaries(self, _order_refs: tuple[str, ...]) -> dict[str, tuple]:
        return {}


def test_origin_filtered_operation_page_scans_past_a_sparse_first_page() -> None:
    """A manual-origin match on page 2 must not be lost behind an empty page 1.

    Regression test: origin filtering is classified in Python from each
    operation's order namespace, not a SQL predicate, so the raw page
    nearest the cursor can legitimately contain zero matches for a sparse
    origin while a match exists further back. Filtering only the first raw
    page (the pre-fix behavior) returned an empty page with a valid
    ``next_cursor`` — a caller reading `rows == []` as "no matches" would
    never discover the manual-origin row without manually paging further.
    """
    manual_ref = build_order_ref(build_manual_order_namespace("operator"), "intent-1")
    strategy_op = _projected_operation(
        effect_operation_id="op-strategy-1",
        strategy_instance_id="spy-bot",
        order_ref="learn-ai/spy-bot/v1:intent-1",
        created_at_ms=2_000,
    )
    manual_op = _projected_operation(
        effect_operation_id="op-manual-1",
        strategy_instance_id=None,
        order_ref=manual_ref,
        created_at_ms=1_000,
        subject_id="manual-operator:operator",
        custody_subject_kind="MANUAL_OPERATOR",
    )
    assert classify_transaction_origin(
        order_ref=manual_ref,
        strategy_instance_id=None,
        custody_subject_kind="MANUAL_OPERATOR",
    ) == "manual"
    pages = [
        OperationPage(
            account_id="PA-ACCOUNT-DESK", strategy_instance_id=None,
            authority_generation=1, control_revision=1, high_water_sequence=2,
            operations=(strategy_op,), next_cursor="cursor-1",
        ),
        OperationPage(
            account_id="PA-ACCOUNT-DESK", strategy_instance_id=None,
            authority_generation=1, control_revision=1, high_water_sequence=2,
            operations=(manual_op,), next_cursor=None,
        ),
    ]
    reader = _StubReader(pages)

    rows, page, scanned = _origin_filtered_operation_page(
        reader,  # type: ignore[arg-type]
        _StubEconomicReader(),  # type: ignore[arg-type]
        account_id="PA-ACCOUNT-DESK",
        origin="manual",
        strategy_instance_id=None,
        lifecycle_state=None,
        run_id=None,
        cursor=None,
        limit=1,
    )

    assert reader.calls == 2
    assert [row.transaction_id for row in rows] == ["op-manual-1"]
    assert page.next_cursor is None
    assert len(scanned) == 1
