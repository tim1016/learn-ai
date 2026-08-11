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
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.adapter import from_alpaca_trade_update
from app.engine.live.order_identity import OwnershipRung, classify_ownership

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


def _project_fixture(_fixture_input: Mapping[str, Any]) -> Mapping[str, Any]:
    """S1/S2 seam for replaying a fixture through SQLite authority folds.

    The ledger, correction fold, external-order observation, and economic
    projections do not exist at S0. Keeping the future seam explicit means the
    golden values cannot silently become assertions over hand-built data.
    """
    raise NotImplementedError("S1/S2 implement the SQLite execution authority projection")


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
            assert actual_projection[collection] == expected_projection[collection]


@pytest.mark.parametrize("family", _FIXTURE_FAMILIES)
def test_execution_authority_fixture_inputs_preserve_slice_evidence(family: str) -> None:
    fixture_input, _ = _load_fixture(family)
    _assert_execution_frames_are_slice_evidence(fixture_input)


def test_execution_authority_external_fixture_is_not_bot_owned() -> None:
    fixture_input, _ = _load_fixture("external_order")
    _assert_external_order_is_not_bot_owned(fixture_input)


@pytest.mark.xfail(reason="S1/S2 implement the authority", strict=True)
@pytest.mark.parametrize("family", _FIXTURE_FAMILIES)
def test_execution_authority_golden(family: str) -> None:
    fixture_input, expected = _load_fixture(family)
    actual = _project_fixture(fixture_input)
    _assert_projection(actual, expected)
