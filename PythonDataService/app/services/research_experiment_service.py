"""The Research Lab's legacy experiment history, read from Postgres.

``"ResearchExperiments"`` and ``"SignalExperiments"`` are tables the retired C# backend created and filled; nothing
writes them now, so this module only reads. A row's ticker is a ``"Tickers"`` row, matched by symbol (upper-cased, as the
C# service did). A row's ``"CreatedAt"`` leaves as ``int64 ms UTC``, floored to the millisecond.

A signal experiment's full report is stored as JSON text. ``parse_signal_report`` reads it into the typed report and
refuses, with ``StoredReportUnreadable``, anything it cannot read faithfully: text that is not JSON, JSON holding NaN or
Infinity (not JSON, and a report cannot serve them), a value that is not an object, a field of the wrong type.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, NoReturn, Protocol

from pydantic import ValidationError

from app.schemas.research_experiments import (
    ResearchExperimentResponse,
    SignalExperimentReport,
    SignalExperimentResponse,
)
from app.services.postgres_connection import pooled_connection

_CREATED_AT_MS = 'FLOOR(EXTRACT(EPOCH FROM e."CreatedAt") * 1000)::bigint'

_RESEARCH_EXPERIMENTS = f"""
    SELECT e."Id", t."Symbol", e."FeatureName", e."StartDate", e."EndDate", e."BarsUsed",
           e."MeanIC", e."ICTStat", e."ICPValue", e."AdfPValue", e."KpssPValue",
           e."IsStationary", e."PassedValidation", e."MonotonicityRatio", e."IsMonotonic",
           {_CREATED_AT_MS} AS created_at_ms
    FROM public."ResearchExperiments" e
    JOIN public."Tickers" t ON t."Id" = e."TickerId"
    WHERE t."Symbol" = $1 AND ($2::text IS NULL OR e."FeatureName" = $2)
    ORDER BY e."CreatedAt" DESC, e."Id" DESC
"""
_SIGNAL_EXPERIMENTS = f"""
    SELECT e."Id", t."Symbol", e."FeatureName", e."StartDate", e."EndDate", e."BarsUsed",
           e."OverallGrade", e."StatusLabel", e."OverallPassed",
           e."MeanOosSharpe", e."BestThreshold", e."BestCostBps", e."FlipSign", e."RegimeGateEnabled",
           {_CREATED_AT_MS} AS created_at_ms
    FROM public."SignalExperiments" e
    JOIN public."Tickers" t ON t."Id" = e."TickerId"
    WHERE t."Symbol" = $1
    ORDER BY e."CreatedAt" DESC, e."Id" DESC
"""
_SIGNAL_REPORT = 'SELECT "JsonReport" FROM public."SignalExperiments" WHERE "Id" = $1'


class StoredReportUnreadable(ValueError):
    """A stored signal report that cannot be read faithfully. The message says why, without echoing the report."""


def research_experiment_from_row(row: Mapping[str, Any]) -> ResearchExperimentResponse:
    return ResearchExperimentResponse(
        id=row["Id"],
        ticker=row["Symbol"],
        feature_name=row["FeatureName"],
        start_date=row["StartDate"],
        end_date=row["EndDate"],
        bars_used=row["BarsUsed"],
        mean_ic=float(row["MeanIC"]),
        ic_t_stat=float(row["ICTStat"]),
        ic_p_value=float(row["ICPValue"]),
        adf_p_value=float(row["AdfPValue"]),
        kpss_p_value=float(row["KpssPValue"]),
        is_stationary=row["IsStationary"],
        passed_validation=row["PassedValidation"],
        monotonicity_ratio=float(row["MonotonicityRatio"]),
        is_monotonic=row["IsMonotonic"],
        created_at_ms=row["created_at_ms"],
    )


def signal_experiment_from_row(row: Mapping[str, Any]) -> SignalExperimentResponse:
    return SignalExperimentResponse(
        id=row["Id"],
        ticker=row["Symbol"],
        feature_name=row["FeatureName"],
        start_date=row["StartDate"],
        end_date=row["EndDate"],
        bars_used=row["BarsUsed"],
        overall_grade=row["OverallGrade"],
        status_label=row["StatusLabel"],
        overall_passed=row["OverallPassed"],
        mean_oos_sharpe=float(row["MeanOosSharpe"]),
        best_threshold=float(row["BestThreshold"]),
        best_cost_bps=float(row["BestCostBps"]),
        flip_sign=row["FlipSign"],
        regime_gate_enabled=row["RegimeGateEnabled"],
        created_at_ms=row["created_at_ms"],
    )


def _refuse_constant(name: str) -> NoReturn:
    raise ValueError(f"the report holds {name}, which is not JSON")


def parse_signal_report(stored: str) -> SignalExperimentReport:
    """The typed report in a stored ``JsonReport`` text, or ``StoredReportUnreadable`` saying why not."""
    try:
        document = json.loads(stored, parse_constant=_refuse_constant)
    except ValueError as exc:
        raise StoredReportUnreadable(f"the stored report is not valid JSON ({exc})") from exc
    if not isinstance(document, dict):
        raise StoredReportUnreadable(f"the stored report is a JSON {type(document).__name__}, not an object")
    try:
        return SignalExperimentReport.model_validate(document)
    except ValidationError as exc:
        first = exc.errors(include_input=False, include_url=False)[0]
        where = ".".join(str(part) for part in first["loc"])
        raise StoredReportUnreadable(
            f"the stored report does not match the signal report shape ({exc.error_count()} problems; "
            f"first at '{where}': {first['msg']})"
        ) from exc


class ResearchExperimentStore(Protocol):
    """What the routes need from the experiment history; the routes inject it so tests can stand in for Postgres."""

    async def research_experiments(self, ticker: str, feature_name: str | None) -> list[ResearchExperimentResponse]: ...

    async def signal_experiments(self, ticker: str) -> list[SignalExperimentResponse]: ...

    async def signal_report(self, experiment_id: int) -> SignalExperimentReport | None: ...


class PostgresResearchExperimentStore:
    """The history over the shared Postgres. Lists are newest first; a ``None`` report means no experiment has that id."""

    async def research_experiments(self, ticker: str, feature_name: str | None) -> list[ResearchExperimentResponse]:
        async with pooled_connection() as conn:
            rows = await conn.fetch(_RESEARCH_EXPERIMENTS, ticker.upper(), feature_name)
        return [research_experiment_from_row(row) for row in rows]

    async def signal_experiments(self, ticker: str) -> list[SignalExperimentResponse]:
        async with pooled_connection() as conn:
            rows = await conn.fetch(_SIGNAL_EXPERIMENTS, ticker.upper())
        return [signal_experiment_from_row(row) for row in rows]

    async def signal_report(self, experiment_id: int) -> SignalExperimentReport | None:
        async with pooled_connection() as conn:
            stored = await conn.fetchval(_SIGNAL_REPORT, experiment_id)
        return None if stored is None else parse_signal_report(stored)
