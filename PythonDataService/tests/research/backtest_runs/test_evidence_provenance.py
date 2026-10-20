"""A run's closing-bar convention is a recorded producer convention (#2607).

Before #2607 a non-LEAN backtest filled a decision taken on the session's
closing bar at that bar's close; since, it sets such a decision aside, as live
does. Evidence records which convention produced it -- and every decision the
convention set aside -- so a record that does not say is classified, never
silently compared with one that does.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.lean_sidecar.closing_bar import ClosingBarConvention
from app.research.backtest_runs.evidence_provenance import RunEvidenceProvenance, assess_evidence_provenance
from app.schemas.backtest_runs import BacktestRunDetailResponse
from tests.research.backtest_runs.test_parity import _run

_CURRENT_BEFORE_2607 = {
    "schema_version": 1,
    "data_contract": "lake_complete_sessions/v1",
    "statistics_basis": "marked_equity_curve/v1",
    "daily_return_convention": "initial_capital_first_session/v1",
}
_SKIP = {"bar_close_ms": 1_770_670_800_000, "intent": "ENTER", "close_price": 693.12}
_QUARANTINE = {
    "bar_start_ms": 1_770_667_200_000,
    "bar_end_ms": 1_770_668_040_000,
    "reason": "TIMEFRAME_MISMATCH",
    "expected_timeframe_ms": 900_000,
}


def test_evidence_that_does_not_record_its_closing_bar_convention_is_unknown_not_current() -> None:
    applicability = assess_evidence_provenance(_CURRENT_BEFORE_2607)

    assert applicability.status == "unknown"
    assert applicability.affected_issues == ()
    assert applicability.requires_manual_override is True
    assert "closing-bar" in applicability.explanation


@pytest.mark.parametrize("convention", list(ClosingBarConvention))
def test_every_convention_a_producer_records_is_current(convention: ClosingBarConvention) -> None:
    applicability = assess_evidence_provenance({**_CURRENT_BEFORE_2607, "closing_bar_convention": convention.value})

    assert applicability.status == "current"
    assert applicability.requires_manual_override is False


def test_a_run_bound_to_a_receipted_lake_snapshot_is_current() -> None:
    """Grid Search cells and Golden Search runs read exactly a receipted snapshot's admitted lake bytes (#2696)."""
    applicability = assess_evidence_provenance(
        {**_CURRENT_BEFORE_2607, "data_contract": "lake_receipted_snapshot/v1", "closing_bar_convention": "skip_closing_bar/v1"}
    )

    assert applicability.status == "current"


@pytest.mark.parametrize(
    ("field", "item", "malformed"),
    [
        ("closing_bar_skips", _SKIP, {"intent": "HOLD"}),
        ("quarantined_decision_bars", _QUARANTINE, {"expected_timeframe_ms": None}),
    ],
    ids=["skipped-decisions", "refused-decision-bars"],
)
def test_the_recorded_decisions_round_trip_with_the_convention(field: str, item: dict, malformed: dict) -> None:
    provenance = RunEvidenceProvenance.model_validate(
        {**_CURRENT_BEFORE_2607, "closing_bar_convention": "skip_closing_bar/v1", field: [item]}
    )

    assert (
        RunEvidenceProvenance.model_validate_json(provenance.model_dump_json()) == provenance
    ), "the recorded decisions survive a JSON round trip"
    with pytest.raises(ValueError):
        RunEvidenceProvenance.model_validate({**_CURRENT_BEFORE_2607, field: [{**item, **malformed}]})


@pytest.mark.parametrize(
    ("stored", "wire_key", "expected"),
    [
        (None, "closingBarSkips", []),
        (json.dumps(_CURRENT_BEFORE_2607), "closingBarSkips", []),
        (
            json.dumps({**_CURRENT_BEFORE_2607, "closing_bar_convention": "skip_closing_bar/v1", "closing_bar_skips": [_SKIP]}),
            "closingBarSkips",
            [{"barCloseMs": 1_770_670_800_000, "intent": "ENTER", "closePrice": 693.12}],
        ),
        (None, "quarantinedDecisionBars", []),
        (json.dumps(_CURRENT_BEFORE_2607), "quarantinedDecisionBars", []),
        (
            json.dumps({**_CURRENT_BEFORE_2607, "quarantined_decision_bars": [_QUARANTINE]}),
            "quarantinedDecisionBars",
            [
                {
                    "barStartMs": 1_770_667_200_000,
                    "barEndMs": 1_770_668_040_000,
                    "reason": "TIMEFRAME_MISMATCH",
                    "expectedTimeframeMs": 900_000,
                }
            ],
        ),
    ],
    ids=[
        "skips-no-provenance",
        "skips-before-2607",
        "skips-lists-the-decisions-the-closing-bar-rule-set-aside",
        "refused-bars-no-provenance",
        "refused-bars-before-2909",
        "refused-bars-lists-the-decision-bars-the-session-refused",
    ],
)
def test_the_run_report_lists_what_the_provenance_recorded(stored: str | None, wire_key: str, expected: list) -> None:
    run = replace(_run(1, "engine"), evidence_provenance_json=stored)

    wire = BacktestRunDetailResponse.model_validate(run).model_dump(by_alias=True)

    assert wire[wire_key] == expected
