"""Acceptance fixtures for the SQLite execution-authority programme (#1441).

The fixture data deliberately captures per-execution websocket evidence, never
cumulative order snapshots. S1/S2 replace ``_project_fixture`` with the
authoritative SQLite capture/fold/projection path and then remove the strict
xfail marker one fixture at a time.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.broker.alpaca.adapter import from_alpaca_trade_update
from app.broker.alpaca.clerk.fifo_pnl import compute_fifo_pnl
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.economic_projection import (
    MarketMark,
    SqliteEconomicProjectionReader,
)
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
from app.broker.alpaca.clerk.sqlite.facts import (
    ExecutionCorrectedFacts,
    ExecutionSliceFilledFacts,
)
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
from app.engine.live.order_identity import OwnershipRung, classify_ownership
from app.lean_sidecar.trading_calendar import SessionWindow
from conftest import _clock_at

_ATOL = 1e-6
_RTOL = 0.0
_FIXTURE_ROOT = Path(__file__).parents[4] / "fixtures" / "golden" / "alpaca-sqlite-execution"
_FIXTURE_FAMILIES = (
    "googl_round_trip",
    "external_order",
    "partial_fill_sequence",
    "downward_correction",
    "null_vs_verified_zero",
)
_S2_FAMILIES = (
    "googl_round_trip",
    "partial_fill_sequence",
    "downward_correction",
    "null_vs_verified_zero",
)


def _load_fixture(family: str) -> tuple[dict[str, Any], dict[str, Any]]:
    fixture_dir = _FIXTURE_ROOT / family
    return (
        json.loads((fixture_dir / "input.json").read_text(encoding="utf-8")),
        json.loads((fixture_dir / "expected.json").read_text(encoding="utf-8")),
    )


def _assert_execution_frames_are_slice_evidence(fixture_input: Mapping[str, Any]) -> None:
    """Guard the S0 premise: every fixture fill is an execution, not a total."""
    for frame in fixture_input.get("trade_updates", []):
        assert frame["stream"] == "trade_updates"
        payload = frame["data"]
        event = from_alpaca_trade_update(payload)
        assert event.execution_id == payload["execution_id"]
        assert event.price is not None
        assert event.quantity is not None
        assert event.quantity > 0

        # The per-execution quantity is deliberately distinct from a cumulative
        # order quantity in the partial-fill family. S1 must consume the former.
        assert "filled_qty" in payload["order"]


def _assert_external_order_is_not_bot_owned(fixture_input: Mapping[str, Any]) -> None:
    if "broker_orders" not in fixture_input:
        return
    order = fixture_input["broker_orders"][0]
    ownership = classify_ownership(
        order_ref=order["client_order_id"],
        perm_id=None,
        exec_id=None,
        allowed_namespaces=frozenset(fixture_input["known_bot_namespaces"]),
        known_intent_ids=frozenset(),
        known_perm_ids=frozenset(),
        known_exec_ids=frozenset(),
    )
    assert ownership is OwnershipRung.NONE


def _fixture_session(fixture_input: Mapping[str, Any]) -> SessionWindow:
    raw_session = fixture_input["session"]
    timezone = ZoneInfo(str(raw_session["timezone"]))
    session_date = datetime.fromtimestamp(
        int(raw_session["session_open_ms"]) / 1000,
        tz=UTC,
    ).astimezone(timezone).date()
    return SessionWindow(
        session_date=session_date,
        open_ms_utc=int(raw_session["session_open_ms"]),
        close_ms_utc=int(raw_session["session_close_ms"]),
    )


def _fixture_leg(
    frames: list[Mapping[str, Any]],
    *,
    client_order_id: str,
) -> BrokerOrderLeg:
    matching = [frame["data"] for frame in frames if frame["data"]["order"]["client_order_id"] == client_order_id]
    first_order = matching[0]["order"]
    return BrokerOrderLeg(
        symbol=str(first_order["symbol"]),
        side=str(first_order["side"]),
        quantity=max(float(item["order"]["filled_qty"]) for item in matching),
    )


def _slice_transition(
    repo: ClerkSqliteRepository,
    accepted: EnterSubmission,
    facts: ExecutionSliceFilledFacts,
) -> TransitionInput:
    return TransitionInput(
        strategy_instance_id=accepted.command.strategy_instance_id,
        run_id=accepted.command.run_id,
        command_id=accepted.command.command_id,
        effect_operation_id=accepted.effect_operation_id,
        order_ref=accepted.order_ref,
        transition_kind="EXECUTION_SLICE_FILLED",
        custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK",
        operation_state="in_progress",
        source_event_at_ms=facts.source_event_at_ms,
        clerk_observed_at_ms=repo.clock(),
        summary_code="EXECUTION_SLICE_FILLED",
        facts_json=facts.to_facts_json(),
    )


def _correction_transition(
    repo: ClerkSqliteRepository,
    accepted: EnterSubmission,
    facts: ExecutionCorrectedFacts,
    *,
    source_event_at_ms: int,
) -> TransitionInput:
    return TransitionInput(
        strategy_instance_id=accepted.command.strategy_instance_id,
        run_id=accepted.command.run_id,
        command_id=accepted.command.command_id,
        effect_operation_id=accepted.effect_operation_id,
        order_ref=accepted.order_ref,
        transition_kind="EXECUTION_CORRECTED",
        custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK",
        operation_state="in_progress",
        source_event_at_ms=source_event_at_ms,
        clerk_observed_at_ms=repo.clock(),
        summary_code="EXECUTION_CORRECTED",
        facts_json=facts.to_facts_json(),
    )


def _project_fixture(
    fixture_input: Mapping[str, Any],
    *,
    tmp_path: Path,
) -> Mapping[str, Any]:
    """Replay one S0 fixture through the S1 ledger and S2 economic reader."""
    session = _fixture_session(fixture_input)
    account_id = str(fixture_input["account_id"])
    repo = ClerkSqliteRepository.initialize(
        account_id=account_id,
        artifacts_root=tmp_path,
        clock=_clock_at(session.open_ms_utc - 1_000),
    )
    try:
        raw_instances = fixture_input.get("strategy_instances")
        if raw_instances is not None:
            instances = [
                item
                for item in raw_instances
                if item.get("economic_projection_available", True)
            ]
        else:
            instances = [fixture_input["strategy_instance"]]

        fixture_to_ledger_sid = {
            str(item["strategy_instance_id"]): f"golden-{index}"
            for index, item in enumerate(instances)
        }
        for item in instances:
            repo.register_strategy_instance(
                strategy_instance_id=fixture_to_ledger_sid[str(item["strategy_instance_id"])],
                symbol=str(item["symbol"]),
                config_hash=f"golden-{item['strategy_instance_id']}",
            )

        frames = list(fixture_input.get("trade_updates", []))
        accepted_by_client_order_id: dict[str, EnterSubmission] = {}
        accepted_by_execution_id: dict[str, EnterSubmission] = {}
        source_time_by_execution_id: dict[str, int] = {}
        execution_order: dict[str, int] = {}
        if frames:
            fixture_strategy_instance_id = str(fixture_input["strategy_instance"]["strategy_instance_id"])
            strategy_instance_id = fixture_to_ledger_sid[fixture_strategy_instance_id]
            submit_start_run(
                repo,
                account_id=account_id,
                strategy_instance_id=strategy_instance_id,
                lifecycle_run_id="golden-fixture-run",
            )
            for frame in frames:
                payload = frame["data"]
                client_order_id = str(payload["order"]["client_order_id"])
                accepted = accepted_by_client_order_id.get(client_order_id)
                if accepted is None:
                    accepted = accept_enter(
                        repo,
                        account_id=account_id,
                        strategy_instance_id=strategy_instance_id,
                        decision_id=f"golden-{len(accepted_by_client_order_id)}",
                        lifecycle_run_id="golden-fixture-run",
                        leg=_fixture_leg(frames, client_order_id=client_order_id),
                    )
                    accepted_by_client_order_id[client_order_id] = accepted
                event = from_alpaca_trade_update(payload)
                facts = ExecutionSliceFilledFacts(
                    execution_id=event.execution_id or "",
                    symbol=str(payload["order"]["symbol"]),
                    side=str(payload["order"]["side"]).upper(),
                    slice_qty=event.quantity or 0.0,
                    slice_price=event.price or 0.0,
                    fee=None,
                    fee_fidelity="not_reported",
                    evidence_source="websocket",
                    source_event_at_ms=event.occurred_at_ms,
                )
                outcome = repo.append_execution_slice_if_absent(
                    execution_id=facts.execution_id,
                    order_ref=accepted.order_ref or "",
                    build_transition=lambda accepted=accepted, facts=facts: _slice_transition(
                        repo,
                        accepted,
                        facts,
                    ),
                    build_coverage_conflict=lambda: (_ for _ in ()).throw(
                        AssertionError("golden websocket fixture must not overlap recovery coverage")
                    ),
                )
                assert outcome == "appended"
                accepted_by_execution_id[facts.execution_id] = accepted
                source_time_by_execution_id[facts.execution_id] = facts.source_event_at_ms
                execution_order[facts.execution_id] = len(execution_order)

        for correction_data in fixture_input.get("authority_corrections", []):
            target_execution_id = str(correction_data["superseded_execution_ref"])
            accepted = accepted_by_execution_id[target_execution_id]
            facts = ExecutionCorrectedFacts(
                execution_id=str(correction_data["execution_id"]),
                superseded_execution_ref=target_execution_id,
                symbol=str(correction_data["symbol"]),
                side=str(correction_data["side"]),
                corrected_qty=float(correction_data["corrected_qty"]),
                corrected_price=float(correction_data["corrected_price"]),
                why=str(correction_data["why"]),
            )
            outcome = repo.append_execution_correction_or_raise(
                correction=_correction_transition(
                    repo,
                    accepted,
                    facts,
                    source_event_at_ms=source_time_by_execution_id[target_execution_id],
                ),
                build_uncertainty=lambda reason: (_ for _ in ()).throw(AssertionError(reason)),
            )
            assert outcome == "appended"
            execution_order[facts.execution_id] = len(execution_order)

        marks = {
            str(symbol): MarketMark(
                price=float(price),
                observed_at_ms=session.close_ms_utc - 1,
            )
            for symbol, price in fixture_input.get("mark_prices", {}).items()
        }
        reader = SqliteEconomicProjectionReader.from_repository(repo)
        try:
            actual_metrics: dict[str, dict[str, Any]] = {}
            for item in raw_instances or [fixture_input["strategy_instance"]]:
                fixture_strategy_instance_id = str(item["strategy_instance_id"])
                strategy_instance_id = fixture_to_ledger_sid.get(fixture_strategy_instance_id)
                snapshot = reader.bot_economic_snapshot(
                    strategy_instance_id or fixture_strategy_instance_id,
                    session_window=session,
                    marks=marks,
                )
                if snapshot is None:
                    actual_metrics[fixture_strategy_instance_id] = {
                        "fills_today": None,
                        "realized_pnl_today": None,
                        "open_pnl": None,
                        "position_quantity": None,
                        "marks_complete": None,
                    }
                    continue
                symbol = str(item["symbol"]).upper()
                actual_metrics[fixture_strategy_instance_id] = {
                    "fills_today": snapshot.fills_today,
                    "realized_pnl_today": snapshot.realized_pnl_today,
                    "open_pnl": snapshot.open_pnl,
                    "position_quantity": snapshot.exposure.get(symbol, 0.0),
                    "marks_complete": snapshot.marks_complete,
                }

            if not frames:
                return {"projection": {"bot_metrics": actual_metrics}}

            fixture_strategy_instance_id = str(fixture_input["strategy_instance"]["strategy_instance_id"])
            strategy_instance_id = fixture_to_ledger_sid[fixture_strategy_instance_id]
            executions = sorted(
                reader.account_executions(bot=strategy_instance_id, limit=100).executions,
                key=lambda row: execution_order[row.execution_id or row.fill_id],
            )
            effective_fills = tuple(
                sorted(
                    reader.bot_fills(strategy_instance_id, limit=100).fills,
                    key=lambda fill: (fill.filled_at_ms, fill.event_key),
                )
            )
        finally:
            reader.close()

        raw_fills = {
            row["execution_id"]: row
            for accepted in accepted_by_client_order_id.values()
            for row in repo.fills_for_order(accepted.order_ref or "")
            if row["execution_id"] is not None
        }
        successor_by_execution_id = {
            row["superseded_execution_ref"]: row["execution_id"]
            for row in raw_fills.values()
            if row["superseded_execution_ref"] is not None
        }
        fills = [
            {
                "fill_id": row.fill_id,
                "execution_id": row.execution_id,
                "strategy_instance_id": fixture_strategy_instance_id,
                "symbol": row.symbol,
                "side": row.side.name,
                "quantity": row.quantity,
                "price": row.price,
                "event_kind": row.event_kind,
                "is_effective": row.state == "effective",
                "superseded_execution_ref": raw_fills[row.execution_id]["superseded_execution_ref"],
                "superseded_by": successor_by_execution_id.get(row.execution_id),
            }
            for row in executions
        ]
        fifo = compute_fifo_pnl(effective_fills)
        closed_lots_by_execution: dict[tuple[str, int], dict[str, Any]] = {}
        for lot in fifo.closed_lots:
            key = (lot.symbol, lot.closed_at_ms)
            closed_lots_by_execution.setdefault(
                key,
                {
                    "strategy_instance_id": fixture_strategy_instance_id,
                    "symbol": lot.symbol,
                    "quantity": 0.0,
                    "realized_pnl": 0.0,
                },
            )
            closed_lots_by_execution[key]["quantity"] += lot.qty
            closed_lots_by_execution[key]["realized_pnl"] += lot.realized_pnl
        closed_lots = list(closed_lots_by_execution.values())
        return {
            "projection": {
                "bot_metrics": actual_metrics,
                "fills": fills,
                "closed_lots": closed_lots,
            }
        }
    finally:
        repo.close()


def _project_external_order_fixture(
    fixture_input: Mapping[str, Any],
    *,
    tmp_path: Path,
) -> Mapping[str, Any]:
    """Replay the external-order fixture through the isolated SQLite fold."""
    observed_at_ms = int(fixture_input["broker_orders"][0]["observed_at_ms"])
    repo = ClerkSqliteRepository.initialize(
        account_id=str(fixture_input["account_id"]),
        artifacts_root=tmp_path,
        clock=_clock_at(observed_at_ms + 1),
    )
    try:
        strategy_instance_ids = tuple(str(value) for value in fixture_input["bots_before"])
        for strategy_instance_id in strategy_instance_ids:
            repo.register_strategy_instance(
                strategy_instance_id=strategy_instance_id,
                symbol="GOOGL",
                config_hash=f"golden-{strategy_instance_id}",
            )
        for raw_order in fixture_input["broker_orders"]:
            observe_external_order(repo, order=_fixture_external_order(raw_order))
        session = SessionWindow(
            session_date=datetime.fromtimestamp(observed_at_ms / 1000, tz=UTC)
            .astimezone(ZoneInfo("America/New_York"))
            .date(),
            open_ms_utc=observed_at_ms - 1,
            close_ms_utc=observed_at_ms + 1,
        )
        reader = SqliteEconomicProjectionReader.from_repository(repo)
        try:
            bot_metrics = {}
            for strategy_instance_id in strategy_instance_ids:
                snapshot = reader.bot_economic_snapshot(
                    strategy_instance_id,
                    session_window=session,
                    marks={},
                )
                assert snapshot is not None
                bot_metrics[strategy_instance_id] = {
                    "fills_today": snapshot.fills_today,
                    "realized_pnl_today": snapshot.realized_pnl_today,
                    "open_pnl": snapshot.open_pnl,
                    "position_quantity": snapshot.exposure.get("GOOGL", 0.0),
                    "marks_complete": snapshot.marks_complete,
                }
        finally:
            reader.close()
        active = repo.active_hold(scope="ACCOUNT_CLERK", reason_code="UNEXPLAINED_ORDER")
        external_orders = [
            {
                "external_order_id": row["external_order_id"],
                "broker_order_id": row["broker_order_id"],
                "client_order_id": row["client_order_id"],
                "symbol": row["symbol"],
                "side": row["side"],
                "qty": row["qty"],
                "price": row["price"],
                "acknowledged_at_ms": row["acknowledged_at_ms"],
            }
            for row in repo.external_orders()
        ]
        return {
            "projection": {
                "bot_metrics": bot_metrics,
                "external_orders": external_orders,
                "active_holds": (
                    []
                    if active is None
                    else [
                        {
                            "scope": active["scope"],
                            "reason_code": active["reason_code"],
                            "state": active["state"],
                            "evidence_refs": json.loads(active["evidence_refs_json"]),
                        }
                    ]
                ),
                "available_actions": [
                    {
                        "action": "ACKNOWLEDGE_EXTERNAL_ORDER",
                        "external_order_id": row["external_order_id"],
                    }
                    for row in repo.external_orders()
                    if row["acknowledged_at_ms"] is None
                ],
            }
        }
    finally:
        repo.close()


def _fixture_external_order(raw_order: Mapping[str, Any]) -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca",
        order_id=str(raw_order["id"]),
        client_order_id=str(raw_order["client_order_id"]),
        symbol=str(raw_order["symbol"]),
        asset_class="us_equity",
        side=str(raw_order["side"]),
        order_type="market",
        time_in_force="day",
        quantity=float(raw_order["qty"]),
        filled_quantity=float(raw_order["filled_qty"]),
        limit_price=None,
        stop_price=None,
        filled_avg_price=(
            float(raw_order["filled_avg_price"])
            if raw_order["filled_avg_price"] is not None
            else None
        ),
        status=str(raw_order["status"]),
        submitted_at_ms=None,
        created_at_ms=None,
        updated_at_ms=int(raw_order["observed_at_ms"]),
        filled_at_ms=None,
        canceled_at_ms=None,
        expired_at_ms=None,
        observed_at_ms=int(raw_order["observed_at_ms"]),
    )


def _assert_optional_float(actual: object, expected: object, *, path: str) -> None:
    if expected is None:
        assert actual is None, f"{path}: expected unavailable (None), got {actual!r}"
        return
    assert actual is not None, f"{path}: expected {expected!r}, got None"
    assert math.isclose(float(actual), float(expected), abs_tol=_ATOL, rel_tol=_RTOL), (
        f"{path}: expected {expected!r}, got {actual!r} "
        f"(atol={_ATOL}, rtol={_RTOL})"
    )


def _assert_metric(actual: Mapping[str, Any], expected: Mapping[str, Any], *, path: str) -> None:
    for field in ("fills_today",):
        expected_count = expected[field]
        actual_count = actual[field]
        if expected_count is None:
            assert actual_count is None, f"{path}.{field}: expected None, got {actual_count!r}"
        else:
            assert isinstance(actual_count, int), f"{path}.{field}: count must be an exact integer"
            assert actual_count == expected_count, f"{path}.{field}: expected {expected_count}, got {actual_count}"

    for field in ("realized_pnl_today", "open_pnl", "position_quantity"):
        _assert_optional_float(actual[field], expected[field], path=f"{path}.{field}")
    assert actual["marks_complete"] is expected["marks_complete"], f"{path}.marks_complete differs"


def _assert_projection(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    """Assert exact authority facts and P&L at the fixture's pinned tolerance."""
    actual_projection = actual["projection"]
    expected_projection = expected["projection"]

    for strategy_instance_id, expected_metric in expected_projection.get("bot_metrics", {}).items():
        _assert_metric(
            actual_projection["bot_metrics"][strategy_instance_id],
            expected_metric,
            path=f"bot_metrics.{strategy_instance_id}",
        )

    for collection in ("fills", "closed_lots", "external_orders", "active_holds", "available_actions"):
        if collection in expected_projection:
            actual_rows = actual_projection[collection]
            expected_rows = expected_projection[collection]
            assert len(actual_rows) == len(expected_rows), f"{collection} row count differs"
            for actual_row, expected_row in zip(actual_rows, expected_rows, strict=True):
                for field, expected_value in expected_row.items():
                    actual_value = actual_row[field]
                    if isinstance(expected_value, float):
                        assert math.isclose(
                            float(actual_value),
                            expected_value,
                            abs_tol=_ATOL,
                            rel_tol=_RTOL,
                        ), (
                            f"{collection}.{field}: expected {expected_value!r}, got {actual_value!r}"
                        )
                    else:
                        assert actual_value == expected_value, (
                            f"{collection}.{field}: expected {expected_value!r}, got {actual_value!r}"
                        )


@pytest.mark.parametrize("family", _FIXTURE_FAMILIES)
def test_execution_authority_fixture_inputs_preserve_slice_evidence(family: str) -> None:
    fixture_input, _ = _load_fixture(family)
    _assert_execution_frames_are_slice_evidence(fixture_input)


def test_execution_authority_external_fixture_is_not_bot_owned() -> None:
    fixture_input, _ = _load_fixture("external_order")
    _assert_external_order_is_not_bot_owned(fixture_input)


@pytest.mark.parametrize(
    "family",
    (*(_S2_FAMILIES), "external_order"),
)
def test_execution_authority_golden(family: str, tmp_path: Path) -> None:
    fixture_input, expected = _load_fixture(family)
    actual = (
        _project_external_order_fixture(fixture_input, tmp_path=tmp_path)
        if family == "external_order"
        else _project_fixture(fixture_input, tmp_path=tmp_path)
    )
    _assert_projection(actual, expected)
