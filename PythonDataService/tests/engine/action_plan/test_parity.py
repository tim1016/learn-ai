"""Slice 1D — pure-function tests for ``parity_diagnostics``.

PRD #593 §"Parity diagnostics" / issue #597: warnings are non-blocking,
hard schema errors stay in Pydantic. This module exercises the pure
function only — the HTTP boundary is tested separately under
``tests/routers/test_preview_action_plan.py``.

Prior art: tests/engine/execution/test_order_sizer.py (pure-function
template for engine helpers).
"""

from __future__ import annotations

import json
from pathlib import Path

from app.engine.action_plan.parity import parity_diagnostics
from app.schemas.action_plan import ActionPlan

_STOCK_LEG: dict = {
    "leg_id": "spy_long",
    "instrument": {"kind": "stock", "underlying": "SPY"},
    "position": "long",
    "qty_ratio": 1,
}


def _plan(**overrides: object) -> ActionPlan:
    return ActionPlan.model_validate(
        {"on_enter": [], "on_exit": [], **overrides}
    )


def test_empty_plan_produces_no_warnings() -> None:
    assert parity_diagnostics(_plan()) == []


def test_symmetric_stock_plan_produces_no_warnings() -> None:
    plan = _plan(
        on_enter=[_STOCK_LEG],
        on_exit=[{"kind": "close_leg", "entry_leg_id": "spy_long"}],
    )

    assert parity_diagnostics(plan) == []


def test_orphan_entry_leg_produces_orphan_entry_warning() -> None:
    """An entry leg with no matching ``close_leg`` warns — the operator
    is declaring a position they have not declared how to close. This is
    a warning, not an error, because calendar / roll plans legitimately
    omit closes."""

    plan = _plan(on_enter=[_STOCK_LEG], on_exit=[])

    warnings = parity_diagnostics(plan)

    assert len(warnings) == 1
    assert warnings[0].code == "orphan_entry"
    assert warnings[0].leg_id == "spy_long"


def test_two_orphan_entries_produce_two_warnings() -> None:
    plan = _plan(
        on_enter=[
            _STOCK_LEG,
            dict(_STOCK_LEG, leg_id="qqq_long", instrument={"kind": "stock", "underlying": "QQQ"}),
        ],
        on_exit=[],
    )

    warnings = parity_diagnostics(plan)

    assert {w.leg_id for w in warnings} == {"spy_long", "qqq_long"}
    assert all(w.code == "orphan_entry" for w in warnings)


def test_parity_diagnostics_does_not_consult_external_state() -> None:
    """Pure-function discipline: the same plan produces the same result
    regardless of how many times it is called or what's in module
    state. Pinned so a future "hint from the registry" optimisation
    doesn't sneak context-dependence into the preview path (PRD #593
    §"Architectural decisions" — parity is a preview-endpoint concern,
    NOT a schema concern, and must NOT consult ``live_config.symbol``)."""

    plan = _plan(on_enter=[_STOCK_LEG], on_exit=[])

    first = parity_diagnostics(plan)
    second = parity_diagnostics(plan)

    assert [(w.code, w.leg_id) for w in first] == [(w.code, w.leg_id) for w in second]


# Cross-language fixture-driven parity check — same JSON files the
# frontend Vitest spec uses via the preview endpoint.

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "action_plan"


def _load(name: str) -> ActionPlan:
    return ActionPlan.model_validate(
        json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    )


def test_parity_symmetric_fixture_has_no_warnings() -> None:
    assert parity_diagnostics(_load("parity_symmetric.json")) == []


def test_parity_orphan_entry_fixture_warns_on_the_open_leg() -> None:
    warnings = parity_diagnostics(_load("parity_orphan_entry.json"))

    assert [w.code for w in warnings] == ["orphan_entry"]
    assert warnings[0].leg_id == "spy_long"
