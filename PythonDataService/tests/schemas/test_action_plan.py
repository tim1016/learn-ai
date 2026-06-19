"""ActionPlan schema tests (PRD #593).

Slice 1A (#594) ships the empty-plan envelope.
Slice 1B (#595) extends the schema with stock ``ActionEntity`` entry
legs and ``close_leg`` ``ExitEntity`` references, plus the cross-language
JSON fixtures under ``tests/fixtures/action_plan/``. Option legs and
selectors land in Slice 1C (#596).

Prior art: ``tests/schemas/test_host_runner_deploy_request_sizing.py``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.action_plan import ActionPlan


def test_empty_action_plan_round_trips() -> None:
    plan = ActionPlan(on_enter=[], on_exit=[])

    assert plan.on_enter == []
    assert plan.on_exit == []
    assert plan.model_dump() == {"on_enter": [], "on_exit": []}


def test_empty_action_plan_constructs_from_defaults() -> None:
    plan = ActionPlan()

    assert plan.on_enter == []
    assert plan.on_exit == []


def test_unknown_top_level_key_rejected() -> None:
    """`extra="forbid"` pins the deploy-boundary invariant: an operator typo
    like ``on_entry`` (instead of ``on_enter``) fails validation instead of
    silently round-tripping a malformed plan into the ledger."""

    with pytest.raises(ValidationError, match=r"on_entry"):
        ActionPlan.model_validate({"on_enter": [], "on_exit": [], "on_entry": []})


# ---------------------------------------------------------------------------
# Slice 1B (#595) — stock entry legs + close_leg exit references.

_STOCK_LEG: dict = {
    "leg_id": "spy_long",
    "instrument": {"kind": "stock", "underlying": "SPY"},
    "position": "long",
    "qty_ratio": 1,
}


def test_single_stock_entry_leg_round_trips() -> None:
    plan = ActionPlan(on_enter=[_STOCK_LEG], on_exit=[])

    assert plan.model_dump() == {"on_enter": [_STOCK_LEG], "on_exit": []}


def test_stock_entry_leg_missing_underlying_rejected() -> None:
    """``instrument.underlying`` is mandatory on every leg — there is no
    implicit fallback from ``live_config.symbol`` (ADR 0012 §5). The plan
    is self-contained or it doesn't validate."""

    bad = {
        "leg_id": "spy_long",
        "instrument": {"kind": "stock"},  # underlying missing
        "position": "long",
        "qty_ratio": 1,
    }
    with pytest.raises(ValidationError, match=r"underlying"):
        ActionPlan(on_enter=[bad], on_exit=[])


def test_stock_entry_leg_qty_ratio_below_one_rejected() -> None:
    """``qty_ratio`` is a positive integer (>= 1) — Slice 1B declarative
    semantics. Composition lands in Slice 4."""

    bad = dict(_STOCK_LEG, qty_ratio=0)
    with pytest.raises(ValidationError, match=r"qty_ratio"):
        ActionPlan(on_enter=[bad], on_exit=[])


def test_stock_entry_leg_missing_leg_id_rejected() -> None:
    bad = {
        "instrument": {"kind": "stock", "underlying": "SPY"},
        "position": "long",
        "qty_ratio": 1,
    }
    with pytest.raises(ValidationError, match=r"leg_id"):
        ActionPlan(on_enter=[bad], on_exit=[])


def test_stock_entry_leg_malformed_leg_id_rejected() -> None:
    """``leg_id`` is constrained to ``^[a-z0-9_]{1,32}$`` so the future
    resolver / persistence layer can rely on it as a stable identifier
    without escaping (ADR 0012 §3)."""

    bad = dict(_STOCK_LEG, leg_id="UPPER-CASE!")
    with pytest.raises(ValidationError, match=r"leg_id"):
        ActionPlan(on_enter=[bad], on_exit=[])


def test_duplicate_leg_ids_within_a_plan_rejected() -> None:
    """Two entry legs cannot share a ``leg_id`` — exits would be
    ambiguous, and the future resolver's ``leg_id → conId`` map would
    collapse."""

    duplicate = dict(_STOCK_LEG)
    with pytest.raises(ValidationError, match=r"duplicate"):
        ActionPlan(on_enter=[_STOCK_LEG, duplicate], on_exit=[])


def test_close_leg_exit_round_trips_when_entry_id_matches() -> None:
    plan = ActionPlan(
        on_enter=[_STOCK_LEG],
        on_exit=[{"kind": "close_leg", "entry_leg_id": "spy_long"}],
    )

    assert plan.model_dump()["on_exit"] == [
        {"kind": "close_leg", "entry_leg_id": "spy_long"}
    ]


def test_orphan_close_leg_referencing_unknown_entry_id_rejected() -> None:
    """A ``close_leg`` referencing a ``leg_id`` that is not in
    ``on_enter`` is a hard schema error, NOT a parity warning (#597
    handles warnings). Future-resolver consumption would fail at runtime;
    catching it at the deploy boundary keeps malformed plans out of the
    ledger."""

    with pytest.raises(ValidationError, match=r"close_leg"):
        ActionPlan(
            on_enter=[_STOCK_LEG],
            on_exit=[{"kind": "close_leg", "entry_leg_id": "does_not_exist"}],
        )


# ---------------------------------------------------------------------------
# Slice 1C (#596) — option entry leg + strike/expiry selectors.

_OPTION_LEG: dict = {
    "leg_id": "spy_long_call",
    "instrument": {"kind": "option", "underlying": "SPY"},
    "position": "long",
    "qty_ratio": 1,
    "right": "call",
    "strike": {"selector": "atm"},
    "expiry": {"selector": "min_dte", "days": 14},
}


def test_single_option_entry_leg_round_trips() -> None:
    plan = ActionPlan(on_enter=[_OPTION_LEG], on_exit=[])

    assert plan.model_dump()["on_enter"] == [_OPTION_LEG]


def test_option_entry_leg_missing_right_rejected() -> None:
    bad = dict(_OPTION_LEG)
    del bad["right"]
    with pytest.raises(ValidationError, match=r"right"):
        ActionPlan(on_enter=[bad], on_exit=[])


def test_option_entry_leg_missing_strike_rejected() -> None:
    bad = dict(_OPTION_LEG)
    del bad["strike"]
    with pytest.raises(ValidationError, match=r"strike"):
        ActionPlan(on_enter=[bad], on_exit=[])


def test_option_entry_leg_missing_expiry_rejected() -> None:
    bad = dict(_OPTION_LEG)
    del bad["expiry"]
    with pytest.raises(ValidationError, match=r"expiry"):
        ActionPlan(on_enter=[bad], on_exit=[])


def test_unknown_strike_selector_rejected() -> None:
    bad = dict(_OPTION_LEG, strike={"selector": "wild_guess"})
    with pytest.raises(ValidationError, match=r"selector"):
        ActionPlan(on_enter=[bad], on_exit=[])


def test_delta_strike_selector_deliberately_unavailable() -> None:
    """ADR 0012 §"Schema shape" / Slice 1C — ``delta`` is hidden from the
    deployable schema until Slice 6 ships its resolver. An operator must
    not be able to deploy a plan the engine cannot run."""

    bad = dict(_OPTION_LEG, strike={"selector": "delta", "target": 0.3})
    with pytest.raises(ValidationError, match=r"selector"):
        ActionPlan(on_enter=[bad], on_exit=[])


def test_atm_offset_selector_round_trips() -> None:
    plan = ActionPlan(
        on_enter=[dict(_OPTION_LEG, strike={"selector": "atm_offset", "offset": 5})],
        on_exit=[],
    )

    assert plan.on_enter[0].strike.selector == "atm_offset"


def test_min_dte_days_below_one_rejected() -> None:
    bad = dict(_OPTION_LEG, expiry={"selector": "min_dte", "days": 0})
    with pytest.raises(ValidationError, match=r"days"):
        ActionPlan(on_enter=[bad], on_exit=[])


def test_absolute_expiry_missing_expiration_ms_rejected() -> None:
    bad = dict(_OPTION_LEG, expiry={"selector": "absolute"})
    with pytest.raises(ValidationError, match=r"expiration_ms"):
        ActionPlan(on_enter=[bad], on_exit=[])


def test_absolute_expiry_round_trips_as_int64_ms() -> None:
    """ADR 0012 §"Schema shape" / repo timestamp policy — wire format is
    ``int64`` ms UTC. Display conversion to ``America/New_York`` lives at
    the UI boundary, not in the schema."""

    plan = ActionPlan(
        on_enter=[
            dict(
                _OPTION_LEG,
                expiry={"selector": "absolute", "expiration_ms": 1_750_000_000_000},
            )
        ],
        on_exit=[],
    )

    assert plan.on_enter[0].expiry.expiration_ms == 1_750_000_000_000


def test_nearest_weekly_round_trips() -> None:
    plan = ActionPlan(
        on_enter=[dict(_OPTION_LEG, expiry={"selector": "nearest_weekly"})],
        on_exit=[],
    )

    assert plan.on_enter[0].expiry.selector == "nearest_weekly"


def test_vertical_spread_two_option_legs_round_trips() -> None:
    """Two option legs sharing expiry — the multi-leg structure
    Slice 1C unblocks. ADR 0012 §3: each leg keeps its own stable
    ``leg_id`` so the future resolver maps each to a distinct
    ``conId`` even when they share strike-resolution inputs."""

    long_call = dict(_OPTION_LEG, leg_id="long_atm_call")
    short_call = dict(
        _OPTION_LEG,
        leg_id="short_otm_call",
        strike={"selector": "atm_offset", "offset": 5},
    )

    plan = ActionPlan(
        on_enter=[long_call, short_call],
        on_exit=[
            {"kind": "close_leg", "entry_leg_id": "long_atm_call"},
            {"kind": "close_leg", "entry_leg_id": "short_otm_call"},
        ],
    )

    assert len(plan.on_enter) == 2
    assert len(plan.on_exit) == 2
