"""A stored signal experiment's report over HTTP, without a database.

These run on every PR: what the route is asked and refuses, what an absent or unreadable report is told, the 503 of a
service with no database configured, and - the heart of it - that a signal report the engine stored is served field for
field. The SQL is proved against a real database in ``test_research_experiments_postgres.py``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from app.config import settings
from app.main import app
from app.routers.research_experiments import get_experiment_store
from app.schemas.research_experiments import SignalExperimentReport
from app.services.research_experiment_service import StoredReportUnreadable, parse_signal_report
from tests._helpers.stored_signal_report import a_busy_signal_report, served, stored_text

BASE = "/api/research"


class _Experiments:
    """A store holding fixed history; a report is parsed from its stored text by the real parser."""

    def __init__(self) -> None:
        self.reports: dict[int, str] = {}
        self.asked: list[tuple[Any, ...]] = []

    async def signal_report(self, experiment_id: int) -> SignalExperimentReport | None:
        self.asked.append(("report", experiment_id))
        stored = self.reports.get(experiment_id)
        return None if stored is None else parse_signal_report(stored)


@pytest.fixture
def history() -> Iterator[_Experiments]:
    store = _Experiments()
    app.dependency_overrides[get_experiment_store] = lambda: store
    yield store
    app.dependency_overrides.pop(get_experiment_store, None)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


# ── The report ─────────────────────────────────────────────────────


def test_a_stored_report_is_served_field_for_field_in_the_engines_own_names() -> None:
    report = a_busy_signal_report()

    assert parse_signal_report(stored_text(report)).model_dump() == served(report)


def test_historical_inference_is_not_relabeled_as_the_new_method() -> None:
    content = json.loads(stored_text(a_busy_signal_report()))
    content["oos_sharpe_ci"].pop("method")
    content["deflated_sharpe"].pop("method")
    restored = parse_signal_report(json.dumps(content))
    assert restored.oos_sharpe_ci.method == "legacy_neff"
    assert restored.deflated_sharpe.method == "legacy_proxy"


async def test_the_report_route_answers_the_stored_report(client: httpx.AsyncClient, history: _Experiments) -> None:
    report = a_busy_signal_report()
    history.reports[41] = stored_text(report)

    response = await client.get(f"{BASE}/signal-experiments/41/report")

    assert response.status_code == 200, response.text
    assert response.json() == served(report)


def test_the_two_keyed_objects_are_served_as_lists_of_entries_in_stored_order() -> None:
    parsed = parse_signal_report(stored_text(a_busy_signal_report()))

    assert [(e.regime, e.count) for e in parsed.regime_coverage] == [("low_vol_up", 40), ("high_vol_down", 12)]
    assert parsed.graduation is not None
    assert parsed.graduation.parameter_stability is not None
    assert [(e.threshold, e.sharpe) for e in parsed.graduation.parameter_stability.sharpe_values_by_threshold] == [
        (0.5, 0.4),
        (1.0, 0.9),
        (1.5, 0.7),
    ]


def test_a_report_stored_by_an_older_engine_reads_with_the_engines_defaults_where_it_has_no_field() -> None:
    stored = json.dumps(
        {
            "success": True,
            "ticker": "SPY",
            "feature_name": "momentum_5m",
            "start_date": "2024-01-02",
            "end_date": "2024-06-28",
            "bars_used": 100,
            "best_threshold": 1.0,
            "backtest_grid": [{"threshold": 1.0, "cost_bps": 2.0, "net_sharpe": 0.4}],
            "graduation": {"overall_grade": "C", "criteria": [{"name": "sharpe", "passed": True}]},
        }
    )

    parsed = parse_signal_report(stored)

    assert (parsed.success, parsed.ticker, parsed.bars_used, parsed.best_threshold) == (True, "SPY", 100, 1.0)
    assert (
        parsed.backtest_grid[0].net_sharpe,
        parsed.backtest_grid[0].total_trades,
        parsed.backtest_grid[0].dates,
    ) == (0.4, 0, [])
    assert parsed.graduation is not None
    assert (parsed.graduation.overall_grade, parsed.graduation.status_label, parsed.graduation.criteria[0].label) == (
        "C",
        "Exploratory",
        "Fail",
    )
    # A section the old engine never wrote is absent, not an empty one passing for a measurement.
    assert (parsed.walk_forward, parsed.graduation.stage_info, parsed.deflated_sharpe, parsed.methodology) == (
        None,
        None,
        None,
        None,
    )
    assert (parsed.regime_coverage, parsed.joint_regime_coverage, parsed.error) == ([], [], None)


def test_a_stored_empty_object_is_an_unsuccessful_report_with_nothing_in_it() -> None:
    parsed = parse_signal_report("{}")

    assert (parsed.success, parsed.bars_used, parsed.backtest_grid, parsed.walk_forward) == (False, 0, [], None)


@pytest.mark.parametrize(
    ("stored", "why"),
    [
        ("", "not valid JSON"),
        ("{not json", "not valid JSON"),
        ('{"success": NaN}', "NaN"),
        ('{"best_threshold": Infinity}', "Infinity"),
        ("[]", "not an object"),
        ('"text"', "not an object"),
        ('{"bars_used": "many"}', "bars_used"),
        ('{"backtest_grid": [{"threshold": 1.0}]}', "backtest_grid.0.cost_bps"),
        ('{"walk_forward": {"windows": "none"}}', "walk_forward.windows"),
        ('{"regime_coverage": {"low": "lots"}}', "regime_coverage"),
        (
            '{"graduation": {"parameter_stability": {"sharpe_values_by_threshold": {"high": 1.0}}}}',
            "sharpe_values_by_threshold",
        ),
    ],
)
def test_a_stored_report_that_cannot_be_read_faithfully_is_refused_and_the_reason_is_named(
    stored: str, why: str
) -> None:
    with pytest.raises(StoredReportUnreadable, match=why):
        parse_signal_report(stored)


async def test_an_unreadable_stored_report_is_a_500_that_names_the_experiment_and_does_not_echo_the_report(
    client: httpx.AsyncClient, history: _Experiments
) -> None:
    history.reports[9] = '{"bars_used": "SECRET-LOOKING-VALUE"}'

    response = await client.get(f"{BASE}/signal-experiments/9/report")

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["code"] == "SIGNAL_REPORT_UNREADABLE"
    assert "signal experiment 9" in detail["message"] and "bars_used" in detail["message"]
    assert "SECRET-LOOKING-VALUE" not in response.text


async def test_a_report_no_experiment_has_is_a_404_that_names_the_id(
    client: httpx.AsyncClient, history: _Experiments
) -> None:
    response = await client.get(f"{BASE}/signal-experiments/2147483647/report")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "SIGNAL_EXPERIMENT_NOT_FOUND"
    assert "2147483647" in response.json()["detail"]["message"]


@pytest.mark.parametrize("experiment_id", ["0", "-1", "2147483648", "abc", "1.5"])
async def test_a_report_id_that_cannot_be_a_row_id_is_refused_without_asking_the_database(
    client: httpx.AsyncClient, history: _Experiments, experiment_id: str
) -> None:
    response = await client.get(f"{BASE}/signal-experiments/{experiment_id}/report")

    assert response.status_code == 422
    assert history.asked == []


# ── A service with no database ─────────────────────────────────────


async def test_with_no_database_configured_the_report_route_answers_a_503_without_naming_the_setting(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "POSTGRES_URL", "")

    response = await client.get(f"{BASE}/signal-experiments/1/report")

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "RESEARCH_EXPERIMENTS_STORE_UNAVAILABLE"
    assert "POSTGRES" not in response.text.upper()
