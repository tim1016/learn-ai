"""The one call a study makes after a result or a failure is produced (#3520).

A job or a route hands its work to this module and gets the same work back, which now also writes one record when the
run ends, however it ends: a result, an exception, a cancel, or a failure the work reports itself. The
study's computation is not touched and does not know. Two shapes cover the five studies:

* ``keeps_job_record(study, request, work)`` for a study that runs as a job (Feature, Signal, Cross-sectional);
* ``@keeps_route_record(study, capture, body=...)`` for a study that answers a request (Indicator reliability, Realized
  vs IV).

What is stored is whatever the run carried: the inputs its request validated, the result as delivered, and the result's
own ``data_source`` block when it has one. A result with no such block is stored with none, and the History says "not
recorded"; nothing is inferred to fill it. A result served from the 24-hour result cache is a copy of a run that has its
own record, so it is not recorded again.

A record that cannot be written never fails the study: the failure is logged with its reason, and a job says so in its
own log. A study that ran is reported as having run.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
from fastapi import HTTPException
from pydantic import BaseModel

from app.jobs.progress import CancellationCheck, JobCancelled, ProgressEmitter
from app.jobs.runner import JobWork
from app.research.study_records.models import RecordStatus, StudyRecordConfig, StudyRecordResult, StudyType
from app.research.study_records.specs import STUDY_SPECS
from app.research.study_records.store import StudyRecordStore
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

_NOT_AN_INPUT = frozenset({"job_id", "force", "rerun_of"})
"""Request fields that say how a run was started, not what it was asked: the job's id, the cache switch, the lineage."""


def _unchanged(result: dict[str, Any]) -> dict[str, Any]:
    return result


@dataclass(frozen=True, slots=True)
class RunCapture:
    """What a run's request says, read when the run ends."""

    inputs: dict[str, Any]
    job_id: str | None = None
    rerun_of: str | None = None
    data_identity: str | None = None
    """A digest of the data the request carried, for a study whose request carries its data."""
    package: Callable[[dict[str, Any]], dict[str, Any]] = _unchanged
    """How the stored result is made from the delivered one (Realized vs IV keeps the bars its chart draws)."""


Capture = Callable[[Any], RunCapture]


def capture_request(request: BaseModel) -> RunCapture:
    """The capture of a request model: every field but the ones that only say how the run was started."""
    return RunCapture(
        inputs=request.model_dump(mode="json", exclude=set(_NOT_AN_INPUT)),
        job_id=getattr(request, "job_id", None),
        rerun_of=getattr(request, "rerun_of", None),
    )


def to_wire_json(value: Any) -> Any:
    """``value`` as JSON can hold it: a number that is not finite is ``null``, numpy scalars are Python's, models are dumped.

    JSON has no NaN, and a result that reaches the file as ``NaN`` is not JSON to anything but Python. Anything else
    JSON cannot hold becomes its string, the way the job store's own ``default=str`` stores it.
    """
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, np.generic):
        return to_wire_json(value.item())
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, BaseModel):
        return to_wire_json(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): to_wire_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_wire_json(item) for item in value]
    return str(value)


def _data_source_of(result: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    block = None if result is None else result.get("data_source")
    return block if isinstance(block, Mapping) else None


def _served_from_cache(result: Mapping[str, Any]) -> bool:
    cache = (_data_source_of(result) or {}).get("cache")
    return isinstance(cache, Mapping) and cache.get("hit") is True


_Outcome = tuple[RecordStatus, str | None, Mapping[str, Any] | None]
"""How a run ended: its status, the detail the History shows for it, and the result it delivered (``None`` for none)."""


class RunRecorder:
    """Writes the record of one run, once, when it ends."""

    def __init__(
        self,
        study: StudyType,
        request: Any,
        capture: Capture,
        *,
        root: Path | None = None,
    ) -> None:
        self._study = study
        self._request = request
        self._capture = capture
        self._root = root
        self._started_ms = now_ms_utc()
        self._ended = False

    def deliver(self, result: Mapping[str, Any]) -> str | None:
        """The run produced ``result``. Returns what to tell the user if it could not be recorded, else ``None``."""

        def settled() -> _Outcome | None:
            wire = to_wire_json(result)
            if _served_from_cache(wire):
                logger.info(
                    "a result served from the cache is not recorded again",
                    extra={"action": "study_record_skipped_cache_hit", "study": self._study},
                )
                return None
            status, detail = STUDY_SPECS[self._study].settle(wire)
            return status, detail, wire

        return self._keep(settled)

    def fail(self, reason: str) -> str | None:
        """The run ended without a result."""
        return self._keep(lambda: ("failed", reason, None))

    def cancel(self, reason: str) -> str | None:
        return self._keep(lambda: ("cancelled", reason, None))

    def _keep(self, settle: Callable[[], _Outcome | None]) -> str | None:
        """Write the record ``settle`` describes, once. Everything that can raise is inside the guard, ``settle`` included."""
        if self._ended:
            return None
        self._ended = True
        status: RecordStatus | None = None
        try:
            outcome = settle()
            if outcome is None:
                return None
            status, detail, wire = outcome
            captured = self._capture(self._request)
            store = StudyRecordStore(self._study, root=self._root)
            record_id = uuid4().hex
            payload = None if wire is None else captured.package(dict(wire))
            data_source = _data_source_of(wire)
            config = StudyRecordConfig(
                record_id=record_id,
                study=self._study,
                created_at_ms=self._started_ms,
                completed_at_ms=max(self._started_ms, now_ms_utc()),
                status=status,
                status_detail=detail,
                headline=self._headline(wire),
                inputs=to_wire_json(captured.inputs),
                data_source=None if data_source is None else dict(data_source),
                data_identity=captured.data_identity,
                job_id=captured.job_id,
                rerun_of=self._known(store, captured.rerun_of),
            )
            store.save(config, StudyRecordResult(record_id=record_id, result=payload))
        except Exception as exc:  # a record that cannot be written must not fail the study; it is logged and said
            logger.exception(
                "a study run could not be recorded",
                extra={"action": "study_record_failed", "study": self._study, "status": status, "error_type": type(exc).__name__},
            )
            return f"This run could not be saved to the History ({type(exc).__name__}: {exc})."
        logger.info(
            "a study run was recorded",
            extra={"action": "study_record_kept", "study": self._study, "record_id": record_id, "status": status},
        )
        return None

    def _headline(self, wire: Mapping[str, Any] | None) -> str | None:
        if wire is None or wire.get("success") is False:
            return None
        try:
            return STUDY_SPECS[self._study].headline(wire)
        except (KeyError, TypeError, ValueError, AttributeError):
            logger.warning(
                "a result's headline could not be built; its record has none",
                extra={"action": "study_record_headline_unavailable", "study": self._study},
                exc_info=True,
            )
            return None

    def _known(self, store: StudyRecordStore, rerun_of: str | None) -> str | None:
        """The record a rerun names, kept only if this store holds it: lineage that points nowhere is not lineage."""
        if rerun_of is None or store.exists(rerun_of):
            return rerun_of
        logger.warning(
            "a rerun names a record the store does not hold; recorded without the link",
            extra={"action": "study_record_rerun_unknown", "study": self._study, "rerun_of": rerun_of},
        )
        return None


# ── A job ───────────────────────────────────────────────────────────────────────────────────────


class _WatchedEmitter:
    """The job's emitter, which also records a failure the work reports itself instead of raising it."""

    def __init__(self, emit: ProgressEmitter, run: RunRecorder) -> None:
        self._emit = emit
        self._run = run

    def failed(self, code: str, message: str) -> None:
        _say(self._emit, self._run.fail(message))
        self._emit.failed(code, message)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._emit, name)


def _say(emit: ProgressEmitter, problem: str | None) -> None:
    if problem is not None:
        emit.log(problem, level="warn")


def keeps_job_record(study: StudyType, request: BaseModel, work: JobWork, *, root: Path | None = None) -> JobWork:
    """``work``, which also records the run when it ends, however it ends. Pass the result to ``run_in_thread`` as ``work``."""

    def recorded(emit: ProgressEmitter, cancel: CancellationCheck) -> Any:
        run = RunRecorder(study, request, capture_request, root=root)
        try:
            result = work(_WatchedEmitter(emit, run), cancel)  # type: ignore[arg-type]
        except JobCancelled as exc:
            _say(emit, run.cancel(str(exc) or "Cancelled on request."))
            raise
        except Exception as exc:
            _say(emit, run.fail(str(exc) or type(exc).__name__))
            raise
        if result is not None:
            _say(emit, run.deliver(result))
        return result

    return recorded


# ── A route ─────────────────────────────────────────────────────────────────────────────────────


def _detail_text(detail: Any) -> str:
    return detail if isinstance(detail, str) else json.dumps(detail, default=str)


def keeps_route_record(
    study: StudyType, capture: Capture, *, body: str, root: Path | None = None
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Decorate a route handler so a run it completes or fails is recorded. ``body`` names the request parameter.

    Put it directly under the route decorator. The handler is called exactly as before and its answer or its exception
    reaches FastAPI unchanged; a request FastAPI refuses before the handler runs (a 422) never ran, so it leaves no record.
    """

    def decorate(handler: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        # FastAPI reads the signature and resolves its string annotations in the function's own module, which would
        # be this one; resolving them here, in the handler's, keeps the route exactly as it was declared.
        signature = inspect.signature(handler, eval_str=True)

        @functools.wraps(handler)
        async def kept(*args: Any, **kwargs: Any) -> Any:
            run = RunRecorder(study, signature.bind(*args, **kwargs).arguments[body], capture, root=root)
            try:
                response = await handler(*args, **kwargs)
            except HTTPException as exc:
                await asyncio.to_thread(run.fail, _detail_text(exc.detail))
                raise
            except Exception as exc:
                await asyncio.to_thread(run.fail, f"{type(exc).__name__}: {exc}")
                raise
            await asyncio.to_thread(run.deliver, response.model_dump(mode="json") if isinstance(response, BaseModel) else response)
            return response

        kept.__signature__ = signature  # type: ignore[attr-defined]
        return kept

    return decorate
