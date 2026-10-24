"""A stored signal experiment's report, read from Postgres.

``"SignalExperiments"`` is a table the retired C# backend created and filled; nothing writes it now, so this module only
reads. Its rows are listed by the Research History (``app.research.history.legacy_sources``).

A signal experiment's full report is stored as JSON text. ``parse_signal_report`` reads it into the typed report and
refuses, with ``StoredReportUnreadable``, anything it cannot read faithfully: text that is not JSON, JSON holding NaN or
Infinity (not JSON, and a report cannot serve them), a value that is not an object, a field of the wrong type.
"""

from __future__ import annotations

import json
from typing import NoReturn, Protocol

from pydantic import ValidationError

from app.schemas.research_experiments import SignalExperimentReport
from app.services.postgres_connection import pooled_connection

_SIGNAL_REPORT = 'SELECT "JsonReport" FROM public."SignalExperiments" WHERE "Id" = $1'


class StoredReportUnreadable(ValueError):
    """A stored signal report that cannot be read faithfully. The message says why, without echoing the report."""


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
    """What the route needs from the stored experiments; the routes inject it so tests can stand in for Postgres."""

    async def signal_report(self, experiment_id: int) -> SignalExperimentReport | None: ...


class PostgresResearchExperimentStore:
    """The reports over the shared Postgres; a ``None`` report means no experiment has that id."""

    async def signal_report(self, experiment_id: int) -> SignalExperimentReport | None:
        async with pooled_connection() as conn:
            stored = await conn.fetchval(_SIGNAL_REPORT, experiment_id)
        return None if stored is None else parse_signal_report(stored)
