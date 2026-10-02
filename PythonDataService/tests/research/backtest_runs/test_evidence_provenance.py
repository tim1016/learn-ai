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


def test_the_skipped_decisions_round_trip_with_the_convention() -> None:
    provenance = RunEvidenceProvenance.model_validate(
        {**_CURRENT_BEFORE_2607, "closing_bar_convention": "skip_closing_bar/v1", "closing_bar_skips": [_SKIP]}
    )

    assert RunEvidenceProvenance.model_validate_json(provenance.model_dump_json()) == provenance
    with pytest.raises(ValueError):
        RunEvidenceProvenance.model_validate({**_CURRENT_BEFORE_2607, "closing_bar_skips": [{**_SKIP, "intent": "HOLD"}]})


def test_the_run_report_lists_the_decisions_the_closing_bar_rule_set_aside() -> None:
    provenance = {**_CURRENT_BEFORE_2607, "closing_bar_convention": "skip_closing_bar/v1", "closing_bar_skips": [_SKIP]}
    run = replace(_run(1, "engine"), evidence_provenance_json=json.dumps(provenance))

    wire = BacktestRunDetailResponse.model_validate(run).model_dump(by_alias=True)

    assert wire["closingBarSkips"] == [{"barCloseMs": 1_770_670_800_000, "intent": "ENTER", "closePrice": 693.12}]


@pytest.mark.parametrize("stored", [None, json.dumps(_CURRENT_BEFORE_2607)], ids=["no-provenance", "before-2607"])
def test_a_run_that_recorded_no_skips_lists_none(stored: str | None) -> None:
    run = replace(_run(1, "engine"), evidence_provenance_json=stored)

    wire = BacktestRunDetailResponse.model_validate(run).model_dump(by_alias=True)

    assert wire["closingBarSkips"] == []
