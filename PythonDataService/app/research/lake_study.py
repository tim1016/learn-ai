"""The run every lake-sourced research study shares (#3521; PRD #3490 R12-R15, T05, T09).

A study that computes from the lake does the same things around its own math:
admit the window it names, fold the minute bars to its resolution, look the
result cache up under an identity that includes the admitted bytes, compute,
store, and say where the data came from. When admission refuses, it ends the
job with a typed refusal and the coverage counts. Those steps live here, once.
A study supplies only what is its own:

* an **identity** (:class:`StudyIdentity`): the lake window it reads
  (:class:`~app.research.study_inputs.MinuteWindowIdentity`), the bar length it
  folds to, and whatever else decides its numbers. :class:`FoldedStudyIdentity`
  is the common shape; a study adds its own fields by overriding
  :meth:`FoldedStudyIdentity.study_fields`. Raising
  :class:`~app.research.study_inputs.StudyInputRefusal` while building it
  (a resolution the study can never run on) ends the job before the lake is
  touched.
* a **compute** (:attr:`LakeStudy.compute`): given the admitted, folded bars it
  returns the study's result as a JSON-able dict, and raises if the study's
  math fails. Nothing else about the data is its concern.
* a few **names** (:class:`LakeStudy`): its phase table key, cache namespace,
  calculation version, and the request fields to echo in a refusal.

To adopt it::

    STUDY = LakeStudy(
        job_type="signal_engine",            # a key of app.jobs.phases.JOB_PHASES
        cache_namespace="signal_engine:lake:v1",
        calculation_version="signal-study/2",
        requester="signal-study",
        subject={"ticker": params.symbol},   # echoed in a refusal's payload
        cache_extra={"signal_config": asdict(config)},   # inputs beyond the lake identity
        admit_message="Checking the lake for ...",
        load_message="Loading ... bars from the lake",
        compute=lambda run: serialize(run_signal_engine(run.rows, ..., **run.hooks._asdict())),
    )
    return run_lake_study(STUDY, lambda: my_identity(params), job_id=..., force=..., emit=..., cancel=...)

The job's phase table must name ``admitting_data``, ``loading_bars`` and
``completed``. Blocking: runs on the job's worker thread.

Cache identity (T09): the key is the study's identity, the data root, the
admitted snapshot digest, the calculation version and ``cache_extra`` (kept under
its own ``study`` key, so a study's field can never take the place of the
digest), under a namespace of its own, looked up *after* admission because the
digest needs the bytes. Repaired or refreshed lake data yields a new key; an
entry the provider path wrote can never be found. Nothing is relabelled.

``subject`` is echoed at the top of a refusal's payload, beside the keys the run
sets itself (:data:`RESULT_KEYS`); a subject that names one of those is refused
when the study is built, since it would otherwise replace the run's own answer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, NamedTuple, Protocol

from app.jobs import cache as result_cache
from app.jobs.phases import friendly as friendly_phase
from app.jobs.progress import CancellationCheck, ProgressEmitter
from app.research.runs.hashing import hash_payload
from app.research.study_inputs import (
    FOLD_RULE,
    AdmittedMinuteInputs,
    MinuteWindowIdentity,
    SessionPolicy,
    StudyCoverage,
    StudyInputRefusal,
    admit_minute_inputs,
    bars_as_rows,
    fold_minute_bars,
)
from app.utils.session_anchors import et_midnight_ms

logger = logging.getLogger(__name__)


#: The keys the run sets in a refusal's payload; a study's ``subject`` cannot use them.
RESULT_KEYS = frozenset({"success", "status", "error", "refusal", "data_source"})

#: What a request's ``session`` ("rth" or "extended", as every ticker request spells it) asks the lake reader for.
SESSION_POLICY_FOR_REQUEST: Mapping[str, SessionPolicy] = {"rth": "regular", "extended": "extended"}


class StudyIdentity(Protocol):
    """What the shared run needs of a study's identity: the lake window, the bar length, and a stable digest."""

    @property
    def inputs(self) -> MinuteWindowIdentity: ...

    @property
    def bar_minutes(self) -> int: ...

    def as_dict(self) -> dict[str, Any]: ...

    def digest(self) -> str: ...


@dataclass(frozen=True)
class FoldedStudyIdentity:
    """One symbol's lake window folded to ``bar_minutes``-minute bars, plus what the study adds to its identity."""

    inputs: MinuteWindowIdentity
    bar_minutes: int

    def study_fields(self) -> dict[str, Any]:
        """The study's own identity fields (look-forward, targets, ...); none by default."""
        return {}

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.inputs.as_dict(),
            "bar_minutes": self.bar_minutes,
            "aggregation": FOLD_RULE,
            **self.study_fields(),
        }

    def digest(self) -> str:
        return hash_payload(self.as_dict())


class StudyHooks(NamedTuple):
    """The callbacks a runner takes, bound to the job: ``on_phase``, ``on_log``, ``on_progress``, ``cancel_check``.

    Every one of them checks for a cancel request first, so a study's math stays cancellable without knowing about jobs.
    """

    on_phase: Callable[[str], None]
    on_log: Callable[..., None]
    on_progress: Callable[..., None]
    cancel_check: Callable[[], bool]


@dataclass(frozen=True)
class StudyRun:
    """What a study's compute receives: its identity, the admitted lake bytes, and the folded window bars."""

    identity: StudyIdentity
    admitted: AdmittedMinuteInputs
    #: The window's bars folded to ``identity.bar_minutes`` (``bars_as_rows``: ``timestamp`` is the bar start, ms UTC).
    rows: list[dict[str, float | int]]
    hooks: StudyHooks


@dataclass(frozen=True)
class LakeStudy:
    """What one study supplies; everything else about running it is :func:`run_lake_study`'s."""

    job_type: str
    cache_namespace: str
    calculation_version: str
    requester: str
    #: The request fields a refusal's payload echoes (``ticker``, ``feature_name``, ...).
    subject: Mapping[str, Any]
    admit_message: str
    load_message: str
    #: Returns the study's JSON-able result, or raises when its math fails.
    compute: Callable[[StudyRun], dict[str, Any]]
    #: Inputs beyond the lake identity that decide the result (feature name, configuration): part of the cache key.
    cache_extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        shadowed = RESULT_KEYS & self.subject.keys()
        if shadowed:
            raise ValueError(f"subject names keys the run sets in its own payload: {sorted(shadowed)}")


# ---------------------------------------------------------------------------
# Provenance and cache identity
# ---------------------------------------------------------------------------


def cache_key(
    identity: StudyIdentity,
    admitted: AdmittedMinuteInputs,
    *,
    calculation_version: str,
    extra: Mapping[str, Any],
) -> dict[str, Any]:
    """The cache identity: data policy, the admitted bytes, the scientific inputs and the code that computes them."""
    return {
        "identity": identity.as_dict(),
        "data_root_id": str(admitted.data_root_id),
        "snapshot_digest": admitted.snapshot_digest,
        "calculation_version": calculation_version,
        "study": dict(extra),
    }


def data_source_block(
    identity: StudyIdentity | None,
    *,
    calculation_version: str,
    coverage: StudyCoverage | None,
    admitted: AdmittedMinuteInputs | None = None,
    bars_used: int | None = None,
    cache_hit: bool = False,
    cached_at_ms: int | None = None,
) -> dict[str, Any] | None:
    """The provenance a data-source line shows: the identity read, the admitted bytes, and the coverage counts.

    ``None`` when there is no identity — the request was refused before anything was read, so there is no source to
    name. ``effective_window`` is the first and last session admitted (ET midnight, ``int64 ms UTC``), unlike the
    requested bounds in the identity; ``None`` until admission succeeds. A cache time the cache did not record stays
    ``None`` — unknown is not zero.
    """
    if identity is None:
        return None
    sessions = admitted.window_sessions if admitted is not None else ()
    return {
        "kind": "lake",
        **identity.as_dict(),
        "identity_digest": identity.digest(),
        "calculation_version": calculation_version,
        "data_root_id": str(admitted.data_root_id) if admitted is not None else None,
        "snapshot_digest": admitted.snapshot_digest if admitted is not None else None,
        "corporate_action_versions": dict(admitted.snapshot.corporate_action_versions) if admitted is not None else {},
        "coverage": coverage.as_dict() if coverage is not None else None,
        "minute_bars_read": len(admitted.warmup_bars) + len(admitted.window_bars) if admitted is not None else None,
        "bars_used": bars_used,
        "effective_window": (
            {"start_ms": et_midnight_ms(sessions[0]), "end_ms": et_midnight_ms(sessions[-1])} if sessions else None
        ),
        "cache": {"hit": cache_hit, "cached_at_ms": cached_at_ms},
    }


def refused_result(subject: Mapping[str, Any], refusal: StudyInputRefusal, *, data_source: dict[str, Any] | None) -> dict[str, Any]:
    """What a refused study answers with: the reason, the sessions and counts admission reached, and the source it read.

    A job parks it as its result; a synchronous route returns it as the body of its refusal status. Either way the
    browser reads it with ``readLakeStudyRefusal``. ``subject`` is the request fields to echo (``ticker``, ...); it
    comes first, so the keys the run sets itself (:data:`RESULT_KEYS`) always win over a field of the same name.
    """
    return {
        **subject,
        "success": False,
        "status": "refused",
        "error": refusal.reason,
        "refusal": refusal.as_dict(),
        "data_source": data_source,
    }


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def coverage_line(coverage: StudyCoverage) -> str:
    """The one log line that says what admission did to the lake."""
    window = coverage.window
    return (
        f"Lake coverage: {window.expected} sessions, {window.reused} reused, {window.added} added, "
        f"{window.missing} missing, {window.failed} failed "
        f"(warmup: {coverage.warmup.expected} sessions; artifacts fetched {coverage.artifacts_fetched}, "
        f"reused {coverage.artifacts_reused})"
    )


def announce_phase(
    job_type: str, emit: ProgressEmitter, cancel: CancellationCheck, phase_id: str, message: str | None = None
) -> None:
    """Move the job to a phase and log it in the user-facing words, after honouring a cancel request."""
    cancel.raise_if_cancelled()
    emit.phase(phase_id)
    emit.log(message if message is not None else friendly_phase(job_type, phase_id))


def hooks_for(emit: ProgressEmitter, cancel: CancellationCheck) -> StudyHooks:
    """The callbacks a study runner takes, bound to this job's emitter and cancellation check."""

    def on_phase(phase: str) -> None:
        cancel.raise_if_cancelled()
        emit.phase(phase)

    def on_log(message: str, level: str = "info") -> None:
        emit.log(message, level=level)

    def on_progress(current: int, total: int, unit: str = "windows", message: str | None = None) -> None:
        emit.progress(current=current, total=total, unit=unit, message=message)

    def cancel_check() -> bool:
        # Raises JobCancelled when set; otherwise returns False so call sites can use it as a poll or a sync barrier.
        cancel.raise_if_cancelled()
        return False

    return StudyHooks(on_phase, on_log, on_progress, cancel_check)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def run_lake_study(
    study: LakeStudy,
    make_identity: Callable[[], StudyIdentity],
    *,
    job_id: str,
    force: bool,
    emit: ProgressEmitter,
    cancel: CancellationCheck,
    now_ms: int | None = None,
) -> dict[str, Any] | None:
    """Admit the study's lake inputs, compute, and return the result — or answer a refusal or cache hit itself.

    Returns the result dict for the job runner to complete. Returns ``None``
    when the job's terminal event is already emitted here: a refusal fails the
    job (typed code, plain reason) and parks the refusal payload with its
    counts as the job's result; a cache hit completes it flagged ``cached``.
    Blocking: runs on the job's worker thread.
    """
    identity: StudyIdentity | None = None
    try:
        identity = make_identity()
        return _run(study, identity, job_id=job_id, force=force, emit=emit, cancel=cancel, now_ms=now_ms)
    except StudyInputRefusal as refusal:
        logger.warning(
            "lake study refused",
            extra={
                "action": "lake_study_refused",
                "study": study.job_type,
                "code": refusal.code,
                "subject": dict(study.subject),
            },
        )
        emit.log(refusal.reason, level="warn")
        emit.failed(
            refusal.code,
            refusal.reason,
            result=refused_result(
                study.subject,
                refusal,
                data_source=data_source_block(
                    identity, calculation_version=study.calculation_version, coverage=refusal.coverage
                ),
            ),
        )
        return None


def _run(
    study: LakeStudy,
    identity: StudyIdentity,
    *,
    job_id: str,
    force: bool,
    emit: ProgressEmitter,
    cancel: CancellationCheck,
    now_ms: int | None,
) -> dict[str, Any] | None:
    # The job's own check is throttled for the hot loops inside the math. The run's boundaries (each phase, each
    # session admission reads, the capture) are far apart in time, so each consults the cancel flag and slides the
    # job's lease. The shared capture itself is never interrupted: another reader may be waiting on it.
    at_boundary = replace(cancel, check_every_n=1)
    announce_phase(study.job_type, emit, at_boundary, "admitting_data", study.admit_message)
    admitted = admit_minute_inputs(
        identity.inputs, requester=study.requester, now_ms=now_ms, check_cancelled=at_boundary.raise_if_cancelled
    )
    emit.log(coverage_line(admitted.coverage))

    announce_phase(study.job_type, emit, at_boundary, "loading_bars", study.load_message)
    rows = bars_as_rows(fold_minute_bars(admitted.window_bars, identity.bar_minutes))
    emit.log(f"Loaded {len(rows):,} bars from {len(admitted.window_bars):,} minute bars")
    emit.progress(current=len(rows), total=len(rows), unit="bars", message="bars loaded")

    key = cache_key(identity, admitted, calculation_version=study.calculation_version, extra=study.cache_extra)
    if not force:
        hit = result_cache.lookup(study.cache_namespace, key)
        if hit is not None:
            _, cached = hit
            cached_at = cached.get("cached_at")
            result = {
                **cached["result"],
                "data_source": data_source_block(
                    identity,
                    calculation_version=study.calculation_version,
                    coverage=admitted.coverage,
                    admitted=admitted,
                    bars_used=len(rows),
                    cache_hit=True,
                    cached_at_ms=int(cached_at) if cached_at is not None else None,
                ),
            }
            result_cache.serve_cached_result(
                job_id, study.job_type, {"result": result, "cached_at": cached_at}, announce_start=False
            )
            return None

    scientific = study.compute(StudyRun(identity, admitted, rows, hooks_for(emit, cancel)))

    announce_phase(study.job_type, emit, at_boundary, "completed")
    result_cache.store(study.cache_namespace, key, scientific)
    return {
        **scientific,
        "data_source": data_source_block(
            identity,
            calculation_version=study.calculation_version,
            coverage=admitted.coverage,
            admitted=admitted,
            bars_used=len(rows),
        ),
    }
