"""The chance-approval policy through the real service: enrollment, enforcement, retention (#3015).

Live Postgres only (``POSTGRES_URL_IS_EPHEMERAL=1``). Every test seeds its own thin lake under a symbol of
its own; the enrollment marker a lock writes is real, and pre-enrollment studies are simulated the way the
database holds them — by deleting the marker from a study's results, never by re-deriving it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import asyncpg
import numpy as np
import pytest

from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc
from app.research.golden_search import service, stages
from app.research.golden_search.models import GoldenSearchRefusal
from app.research.persistence import lifecycle
from app.schemas.golden_search import GoldenSearchExamView
from app.utils.session_anchors import et_date_at_ms
from tests._helpers.golden_search_study import (
    CAPITAL,
    FINAL,
    Driver,
    FakeEngine,
    plan_request,
    seed_lake,
    smooth_score,
    unique_symbol,
    window_ms,
)

APPROVE = {
    "note": "Reviewed.",
    "acknowledge_missing_parity": True,
    "acknowledge_research_weakness": False,
    "expected_default_qualification_id": None,
}

#: Windows starting at or after this instant are the recent months and the final interval.
FINAL_FROM_MS = window_ms(FINAL)[0]

APPROVAL_SCHEMA = "golden-search-chance-approval/v1"


@pytest.fixture
def symbol() -> str:
    return unique_symbol()


@pytest.fixture
def lake(tmp_path: Path, symbol: str, conn: asyncpg.Connection) -> Path:
    # ``conn`` is the ephemeral-Postgres guard: where no attested POSTGRES_URL exists (the PR shards), the
    # suite skips here rather than seeding a lake it could never study.
    return seed_lake(tmp_path, symbol)


async def _strip_enrollment(conn: asyncpg.Connection, study_id: str) -> None:
    """Hold a study the way a pre-enrollment row sits in the database: no marker, nothing inferred on read."""
    await conn.execute("UPDATE research_golden_search_studies SET results_json = results_json - 'chance_approval' WHERE id = $1", study_id)


def _dominant_on_the_final_interval(point: dict[str, Any], window: tuple[int, int], scenario: str) -> float:
    """Peaks at gap 0.4 everywhere; the incumbent's own gap 0.2 collapses only on the final interval.

    The searched fit therefore beats the incumbent on every resampled final path (a clear comparison), its
    resampled profit is clearly positive, and the only acknowledgement left to test is the one a test removes.
    """
    gap = float(point.get("gap", 0.2))
    hold = float(point.get("hold_bars", 5))
    cost = {"base": 0.0, "slippage_1c": 0.05, "commission_1": 0.02}.get(scenario, 0.0)
    if window[0] >= FINAL_FROM_MS and abs(gap - 0.2) < 1e-9:
        return 0.5 - cost
    return 2.0 - 8.0 * (gap - 0.4) ** 2 - ((hold - 7.0) / 4.0) ** 2 - cost


class NoisyDetailEngine(FakeEngine):
    """FakeEngine whose detail holds non-constant session returns, so per-path Sharpe comparisons are defined.

    ``fake_detail``'s linear equity ramp gives every session the same return, and a constant series has no
    resampleable Sharpe spread; scaling the noise mean by the score makes each path's Sharpe order the way the
    recorded Sharpe does. The detail for one window is deterministic, so a stored digest still reads back.
    """

    def __call__(self, request: Any) -> Any:
        result = super().__call__(request)
        if request.detail is False or result.detail is None:
            return result
        sharpe = self.score(request.point, request.window, "base" if request.scenario is None else request.scenario.key)
        days = expected_sessions(et_date_at_ms(request.window[0]), et_date_at_ms(request.window[1] - 1))
        rng = np.random.default_rng(request.window[0])
        returns = rng.normal(float(sharpe) * 0.0005, 0.002, len(days))
        equity = [CAPITAL]
        for value in returns:
            equity.append(equity[-1] * (1.0 + value))
        detail = dict(result.detail)
        detail["daily_equity"] = [[session_close_ms_utc(day), value] for day, value in zip(days, equity[1:], strict=True)]
        return replace(result, detail=detail)


def _driver(lake: Path, monkeypatch: pytest.MonkeyPatch, *, score: Any = smooth_score, rehearsals: bool = True) -> Driver:
    """One study driver per test: this lake, and the landscape the test wants, patched in one place."""
    if not rehearsals:
        def no_rehearsal(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("rehearsal computation exploded")

        monkeypatch.setattr(stages, "rehearsal_reading", no_rehearsal)
    monkeypatch.setattr(lifecycle, "roots_for", lambda row: [lake])
    engine: Any = FakeEngine() if score is smooth_score else NoisyDetailEngine(score=score)
    return Driver(roots=[lake], engine=engine)


async def _to_review(driver: Driver, symbol: str) -> Any:
    row = await driver.to_candidate(symbol)
    return await driver.advance(row, "open_exam", {"acknowledge_final_test": True})


def _fit(detail: dict[str, Any], key: str) -> dict[str, Any]:
    return next(fit for fit in detail["results"]["exam"]["fits"] if fit["key"] == key)


async def test_a_lock_enrolls_the_study_and_idempotent_retries_return_the_marker_unchanged(
    lake: Path, monkeypatch: pytest.MonkeyPatch, symbol: str
) -> None:
    driver = _driver(lake, monkeypatch)
    key = driver.key()
    first = await service.lock_study(plan_request(symbol), idempotency_key=key, roots=[lake])
    marker = first.results["chance_approval"]
    assert marker["schema_version"] == APPROVAL_SCHEMA
    assert marker["required_final_components"] == ["comparison", "profitability", "carry_over"]

    again = await service.lock_study(plan_request(symbol), idempotency_key=key, roots=[lake])
    assert again.results["chance_approval"] == marker


async def test_a_clear_complete_fit_approves_without_the_chance_acknowledgement(
    lake: Path, monkeypatch: pytest.MonkeyPatch, symbol: str
) -> None:
    driver = _driver(lake, monkeypatch, score=_dominant_on_the_final_interval)
    row = await _to_review(driver, symbol)
    detail = await driver.detail(row)
    fit = _fit(detail, "all_period")
    assert fit["outcome"] == "meets_rules"
    assert fit["chance"]["comparison"]["status"] == "measured"
    # A clear pass needs no new weakness acknowledgement: nothing weak, nothing unavailable.
    assert [item["code"] for item in fit["weakness"]] == []

    row = await driver.advance(row, "approve", {**APPROVE, "candidate_key": "all_period"})
    assert row.state == "approved"
    research = driver.approval.requests[-1].research
    assert research["weakness"] == []
    assert research["research_override"] is False
    # The immutable qualified version retains the chance evidence either way.
    assert research["chance"]["policy_schema"] == APPROVAL_SCHEMA
    assert research["chance"]["components"]["comparison"]["status"] == "measured"
    assert research["chance"]["weakness"] == []


async def test_an_enrolled_study_refuses_to_approve_a_missing_required_carry_over_until_acknowledged(
    lake: Path, monkeypatch: pytest.MonkeyPatch, symbol: str
) -> None:
    driver = _driver(lake, monkeypatch, score=_dominant_on_the_final_interval, rehearsals=False)
    row = await _to_review(driver, symbol)
    detail = await driver.detail(row)
    fit = _fit(detail, "all_period")
    # The measured comparison is clear; the carry-over is unavailable, and the page names why.
    assert fit["chance"]["carry_over"]["status"] == "missing"
    codes = [item["code"] for item in fit["weakness"]]
    assert codes == ["CHANCE_UNAVAILABLE"]
    assert "carry-over" in fit["weakness"][0]["text"] and "matching stored rehearsal" in fit["weakness"][0]["text"]

    with pytest.raises(GoldenSearchRefusal) as refused:
        await driver.command(row, "approve", {**APPROVE, "candidate_key": "all_period"})
    assert refused.value.code == "WEAKNESS_ACKNOWLEDGEMENT_REQUIRED"
    assert "CHANCE_UNAVAILABLE" in str(refused.value)

    row = await driver.advance(row, "approve", {**APPROVE, "candidate_key": "all_period", "acknowledge_research_weakness": True})
    assert row.state == "approved"
    research = driver.approval.requests[-1].research
    assert research["weakness"] == ["CHANCE_UNAVAILABLE"]
    assert research["research_override"] is True
    assert research["chance"]["components"]["carry_over"]["status"] == "missing"
    assert research["chance"]["weakness"][0]["code"] == "CHANCE_UNAVAILABLE"


async def _mutate_results(conn: asyncpg.Connection, study_id: str, mutate: Any) -> None:
    """Hold a study's stored results the way a corrupted or historical row sits in the database: edited in place, never recomputed (#3024)."""
    current = await conn.fetchrow("SELECT results_json FROM research_golden_search_studies WHERE id = $1", study_id)
    results = json.loads(current["results_json"])
    mutate(results)
    await conn.execute("UPDATE research_golden_search_studies SET results_json = $2 WHERE id = $1", study_id, json.dumps(results))


def _unsupported_comparison_schema(results: dict[str, Any]) -> None:
    """A fully wire-valid stored comparison whose interpretation version no server understands."""
    for fit in results["exam"]["fits"]:
        if fit["key"] == "all_period":
            fit["chance"]["comparison"]["basis"]["schema_version"] = "golden-search-chance-comparison/v999"


async def test_invalid_stored_chance_evidence_requires_the_acknowledgement_and_is_retained(
    lake: Path, monkeypatch: pytest.MonkeyPatch, conn: asyncpg.Connection, symbol: str
) -> None:
    driver = _driver(lake, monkeypatch, score=_dominant_on_the_final_interval)
    row = await _to_review(driver, symbol)
    await _mutate_results(conn, row.id, _unsupported_comparison_schema)

    # The page still serves, wire-valid, with the reason named and the valid siblings visible.
    detail = await driver.detail(row)
    fit = _fit(detail, "all_period")
    assert fit["chance"]["comparison"]["status"] == "missing"
    assert fit["chance"]["comparison"]["code"] == "invalid_record"
    assert "v999" in fit["chance"]["comparison"]["reason"]
    assert fit["chance"]["profitability"]["status"] == "measured"
    assert fit["chance"]["carry_over"]["status"] == "measured"
    GoldenSearchExamView.model_validate(detail["results"]["exam"])
    assert [item["code"] for item in fit["weakness"]] == ["CHANCE_UNAVAILABLE"]
    assert "cannot interpret" in fit["weakness"][0]["text"] or "v999" in fit["weakness"][0]["text"]

    with pytest.raises(GoldenSearchRefusal) as refused:
        await driver.command(row, "approve", {**APPROVE, "candidate_key": "all_period"})
    assert refused.value.code == "WEAKNESS_ACKNOWLEDGEMENT_REQUIRED"

    row = await driver.advance(row, "approve", {**APPROVE, "candidate_key": "all_period", "acknowledge_research_weakness": True})
    assert row.state == "approved"
    research = driver.approval.requests[-1].research
    assert research["candidate_key"] == "all_period"  # a retry of this approval names the same fit
    assert research["weakness"] == ["CHANCE_UNAVAILABLE"]
    assert research["chance"]["policy_schema"] == APPROVAL_SCHEMA
    assert research["chance"]["components"]["comparison"]["code"] == "invalid_record"
    assert "v999" in research["chance"]["components"]["comparison"]["reason"]


async def test_a_pre_enrollment_study_never_gates_on_invalid_stored_evidence(
    lake: Path, monkeypatch: pytest.MonkeyPatch, conn: asyncpg.Connection, symbol: str
) -> None:
    driver = _driver(lake, monkeypatch, score=_dominant_on_the_final_interval)
    row = await _to_review(driver, symbol)
    await _mutate_results(conn, row.id, _unsupported_comparison_schema)
    await _strip_enrollment(conn, row.id)

    detail = await driver.detail(row)
    fit = _fit(detail, "all_period")
    assert fit["chance"]["comparison"]["code"] == "invalid_record"
    assert fit["weakness"] == []  # absent-reading behavior: an invalid record gates nothing without the marker

    row = await driver.advance(row, "approve", {**APPROVE, "candidate_key": "all_period"})
    assert row.state == "approved"
    research = driver.approval.requests[-1].research
    assert research["weakness"] == []
    assert research["chance"]["policy_schema"] is None


async def test_a_pre_enrollment_study_keeps_the_original_absent_reading_behavior(
    lake: Path, monkeypatch: pytest.MonkeyPatch, conn: asyncpg.Connection, symbol: str
) -> None:
    driver = _driver(lake, monkeypatch, score=_dominant_on_the_final_interval, rehearsals=False)
    row = await _to_review(driver, symbol)
    await _strip_enrollment(conn, row.id)

    detail = await driver.detail(row)
    fit = _fit(detail, "all_period")
    # The carry-over is still missing — nothing backfills it — but a pre-enrollment study never gates on it.
    assert fit["chance"]["carry_over"]["status"] == "missing"
    assert fit["weakness"] == []

    row = await driver.advance(row, "approve", {**APPROVE, "candidate_key": "all_period"})
    assert row.state == "approved"
    research = driver.approval.requests[-1].research
    assert research["weakness"] == []
    assert research["research_override"] is False
    assert research["chance"]["policy_schema"] is None


async def test_a_measured_weak_share_still_asks_on_a_pre_enrollment_study(
    lake: Path, monkeypatch: pytest.MonkeyPatch, conn: asyncpg.Connection, symbol: str
) -> None:
    driver = _driver(lake, monkeypatch)
    row = await _to_review(driver, symbol)
    await _strip_enrollment(conn, row.id)
    # The default landscape's final comparison is genuinely weak; owner decision 3 applies with or without
    # enrollment, so the refusal names CHANCE_WITHIN_NOISE even for a study locked before the policy shipped.
    detail = await driver.detail(row)
    codes = [item["code"] for item in _fit(detail, "all_period")["weakness"]]
    assert codes == ["CHANCE_WITHIN_NOISE"]
    with pytest.raises(GoldenSearchRefusal) as refused:
        await driver.command(row, "approve", {**APPROVE, "candidate_key": "all_period"})
    assert refused.value.code == "WEAKNESS_ACKNOWLEDGEMENT_REQUIRED"
    row = await driver.advance(row, "approve", {**APPROVE, "candidate_key": "all_period", "acknowledge_research_weakness": True})
    assert row.state == "approved"
