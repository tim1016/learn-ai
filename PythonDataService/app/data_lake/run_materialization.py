"""Run materialization: the seam where a backtest run asks the lake for bytes.

A backtest run needs LEAN-format bars on disk before the engine can read
them. Historically that was the policy store's ``ensure_range``, which
exported Polygon aggregates into a policy-keyed cache directory. With
``DATA_LAKE_ENABLED`` the same question is answered by the lake:
``ensure_data`` materializes exactly the missing artifacts, the catalog
arbitrates who fetches what, and every run leaves with a fingerprint of the
lake state it materialized against — whose scope :func:`materialize_engine_run`
states canonically.

This module is only the bridge, and its public surface is two symbols:
:func:`materialize_engine_run` and the :class:`LakeMaterializationError` it
raises. Everything else is private, because the call composes three things no
caller should have to solve — or be able to second-guess — for itself:

1. **The spec.** What a Python-engine run actually needs from the lake —
   minute and daily trade bars, and nothing else (see
   :func:`_build_engine_run_spec`).
2. **Contention.** Two runs wanting the same day is normal, not an error.
   The catalog hands the fetch to one of them; the other waits and takes
   the winner's bytes (see :func:`_materialize_run_data`).
3. **The sync boundary.** Backtests run on worker threads with no event
   loop, and the catalog's connection pool is bound to the loop that
   created it (see :func:`_materialize_run_data_sync`).

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from app.data_lake.ensure_data import ensure_data
from app.data_lake.types import (
    ArtifactFailure,
    ArtifactRecord,
    DataAvailabilityResult,
    DataRunSpec,
    PriceAdjustmentMode,
)

logger = logging.getLogger(__name__)

# Mirrors ``EngineBacktestRequest.resolution``: which reader the run will use,
# and therefore which artifact class the coverage gate must insist on.
EngineResolution = Literal["minute", "daily"]


@dataclass(frozen=True, slots=True)
class EngineRunMaterialization:
    """What a backtest run may know about the lake it is about to read.

    Deliberately narrower than ``DataAvailabilityResult``. The failure list is
    judged once, by the coverage gate in :func:`materialize_engine_run`; a run
    that received the raw result could re-derive its own answer from
    ``.failures`` and disagree with the gate about whether it should proceed.
    What is left is the fingerprint to record, two counts worth logging, and a
    pre-rendered line for the operator when the lake reports itself incomplete.
    """

    availability_hash: str
    fetched_artifact_count: int
    reused_artifact_count: int
    # None when the lake produced everything; otherwise a ``kind/reason``
    # summary of what it could not, already judged harmless for this run.
    incomplete_summary: str | None

# "Another worker holds the claim and has not finished yet." Clears by itself.
_CONTENTION_REASONS = frozenset({"lease_timeout"})

# Consequences of the above rather than independent failures: with a leased
# artifact still missing, ensure_data cannot build what derives from it (the
# daily bars aggregated from the day's minute bars) and says so. These clear
# when the lease does, so they must not abort the wait — but on their own,
# with nothing leased elsewhere, they are a real failure.
#
# Known trade-off, accepted for this slice: pairing ``lease_timeout`` with a
# cascade keeps the wait going until ``fetch_timeout_seconds`` runs out, even
# when the lease belongs to a worker that has died — this wait is wall-clock
# bounded, not TTL-aware, so it does not read ``LeaseExpiresAtMs`` and cannot
# short-circuit on an expired lease. Consolidating it with the catalog's own
# lease classifier is booked for the integration slice.
_CASCADED_REASONS = frozenset({"internal_error"})

_CONTENTION_POLL_INTERVAL_S = 0.5


class LakeMaterializationError(RuntimeError):
    """The lake cannot give this run the bytes it asked for."""


def _build_engine_run_spec(
    *,
    symbol: str,
    start: date,
    end: date,
    price_adjustment_mode: PriceAdjustmentMode = "raw",
    requester: str | None = None,
    request_id: UUID | None = None,
    fetch_timeout_seconds: int = 600,
) -> DataRunSpec:
    """Describe what a Python-engine backtest needs from the lake.

    Trade bars only. The Python engine reads minute zips (and the
    per-symbol daily zip, which the lake derives from them) and never
    opens a factor file or a map file — those are LEAN's corp-action
    inputs and belong to the sidecar's spec, not this one. Asking for
    them here would buy two extra provider round-trips per run plus a
    window-keyed data contract that makes the *next* window's run report
    a contract mismatch.
    """
    # Imported here rather than at module scope: the digest is the sidecar
    # package's to publish, and the lake should not depend on it to import.
    from app.lean_sidecar.config import PINNED_LEAN_IMAGE_DIGEST

    if not PINNED_LEAN_IMAGE_DIGEST:
        raise LakeMaterializationError(
            "no pinned LEAN image digest; the lake sources its session calendar from that image "
            "(run scripts/lean_sidecar_pin_image.py)"
        )

    return DataRunSpec(
        request_id=request_id or uuid4(),
        run_type="python_lab",
        requester=requester,
        symbols=[symbol.upper()],
        start_trading_date=start,
        end_trading_date=end,
        data_types=["trade"],
        price_adjustment_mode=price_adjustment_mode,
        include_factor_files=False,
        include_map_files=False,
        lean_image_digest=PINNED_LEAN_IMAGE_DIGEST,
        fetch_timeout_seconds=fetch_timeout_seconds,
    )


def _is_blocked_by_a_sibling_fetch(result: DataAvailabilityResult) -> bool:
    """True when the only thing standing between this run and its bytes is a lease."""
    reasons = {f.reason for f in result.failures}
    if not reasons & _CONTENTION_REASONS:
        return False
    return not (reasons - _CONTENTION_REASONS - _CASCADED_REASONS)


async def _materialize_run_data(
    spec: DataRunSpec,
    *,
    poll_interval_s: float = _CONTENTION_POLL_INTERVAL_S,
    now: Callable[[], float] = time.monotonic,
) -> DataAvailabilityResult:
    """Materialize every artifact ``spec`` needs, waiting out sibling fetches.

    ``ensure_data`` claims each artifact in the catalog before fetching it,
    so of two runs wanting the same day exactly one fetches. The other is
    told ``lease_timeout`` — which is not a failure, it is "come back when
    the winner is done". Re-running the same spec once the winner completes
    turns every one of those into a cache hit, so both runs finish against
    one fetch and the same bytes.

    Waiting is bounded by ``spec.fetch_timeout_seconds``. A result carrying
    a failure a retry cannot fix is returned immediately: polling on a
    missing symbol or a dead launcher only wastes the run's clock.
    """
    deadline = now() + spec.fetch_timeout_seconds
    while True:
        result = await ensure_data(spec)
        if result.overall_status == "complete" or not _is_blocked_by_a_sibling_fetch(result):
            return result

        remaining = deadline - now()
        if remaining <= 0:
            logger.warning(
                "data_lake.run_materialization: gave up waiting on %d contended artifact(s) "
                "for %s after %ds",
                len(result.failures),
                spec.symbols,
                spec.fetch_timeout_seconds,
            )
            return result

        logger.info(
            "data_lake.run_materialization: %d artifact(s) for %s are being fetched by another run; waiting",
            len(result.failures),
            spec.symbols,
        )
        await asyncio.sleep(min(poll_interval_s, remaining))


# ---------------------------------------------------------------------------
# Synchronous boundary
# ---------------------------------------------------------------------------
#
# Backtests are synchronous and execute on worker threads (FastAPI's
# threadpool for the sync route, the Jobs worker's own thread otherwise).
# An asyncpg pool belongs to the event loop that created it, and
# ``catalog_client`` keys its pools by that loop (a bare process-global pool
# used to bind to whichever loop called ``init_pool()`` first, breaking
# every other loop's catalog calls the moment coexistence with
# ``/api/data-lake/*`` — which runs on the FastAPI app loop — actually
# happened; see ``catalog_client``'s own module comment). Loop-awareness
# there means a fresh ``asyncio.run()`` per call would ALSO work correctly
# here now, each call getting a valid pool for its own throwaway loop — but
# it would pay for a brand-new asyncpg pool (real Postgres connections) on
# every single backtest, and never close it, since nothing calls
# ``close_pool()`` on a throwaway loop after the fact. A process-wide lock
# would avoid that churn by serializing everything onto one pool, which is
# worse than what it replaces — the policy store's lock is per symbol, so
# two runs on different symbols never wait on each other today.
#
# So: one long-lived loop for the whole process. The pool is created on it
# once, concurrent runs submit onto it and interleave there, and who
# fetches what stays a question for the catalog rather than for a mutex.
_loop_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None


def _materialization_loop() -> asyncio.AbstractEventLoop:
    """Return the process-wide loop that owns the catalog connection pool."""
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever,
                name="data-lake-materialization",
                daemon=True,
            )
            thread.start()
            _loop = loop
        return _loop


def _materialize_run_data_sync(spec: DataRunSpec) -> DataAvailabilityResult:
    """Blocking :func:`_materialize_run_data` for callers without an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "_materialize_run_data_sync was called from a running event loop; await _materialize_run_data instead"
        )

    future = asyncio.run_coroutine_threadsafe(_materialize_run_data(spec), _materialization_loop())
    # The coroutine's own deadline is fetch_timeout_seconds; allow a small
    # margin so the wait unwinds through the coroutine's return path rather
    # than being cancelled here.
    #
    # Known trade-off, accepted for this slice: if that margin is exceeded the
    # TimeoutError propagates but the coroutine is NOT cancelled — it keeps
    # running on the shared loop until its own deadline, and any artifact it
    # then completes lands in the catalog with no caller waiting for it. That
    # is harmless (the work is idempotent and the row is correct) but it means
    # a timed-out run can still be holding a lease for a while afterwards.
    return future.result(timeout=spec.fetch_timeout_seconds + 30)


def _describe_failures(failures: Iterable[ArtifactFailure]) -> str:
    """One short ``kind/reason`` summary of what the lake could not produce.

    A per-day failure additionally names its trading date
    (``kind/reason@date``) so a refusal spanning a multi-day window says
    which session it means, not just which kind of thing went wrong.
    """
    return "; ".join(
        sorted(
            {f"{f.artifact_kind}/{f.reason}" + (f"@{f.trading_date}" if f.trading_date else "") for f in failures}
        )
    )


def _withholds_bars_the_run_reads(failure: ArtifactFailure, *, resolution: EngineResolution) -> bool:
    """Does this failure hold back bars the run's reader will actually open?

    ``ArtifactFailure`` carries no resolution, but within ``time_series_bars``
    the spec makes the discriminator exact: per-day minute artifacts carry a
    ``trading_date``, and the per-symbol aggregated (daily) artifact does not.

    A minute-resolution run opens only the former, so only a per-day failure
    withholds its bars — a stale or missing daily zip is not this run's
    concern.

    A daily-resolution run is different, and asymmetrically so: the daily zip
    IS every source session's minute bars, aggregated — ``ensure_data``
    derives it from whichever minute artifacts materialized, even when the
    window's source coverage is partial, and a data-contract match against
    that partial set is stable (nothing about the identity changes) if the
    missing session never gets fetched. So a per-day failure withholds a
    daily reader's bars exactly as surely as a failure on the aggregate
    artifact itself does — the alternative is a daily-resolution run
    silently reading a series with a session missing, with no failure on the
    aggregate to ever catch it. Every ``time_series_bars`` failure therefore
    withholds a daily run, unconditionally.

    Metadata failures mean the lake fell back to its hardcoded calendar —
    bad, and logged — but they withhold no bars, at either resolution.
    """
    if failure.artifact_kind != "time_series_bars":
        return False
    if resolution == "daily":
        return True
    return failure.trading_date is not None


def _opened_by_this_run(artifact: ArtifactRecord, *, resolution: EngineResolution) -> bool:
    """Does this run's reader actually open this artifact's file?

    Mirrors :func:`_withholds_bars_the_run_reads`'s discriminator. Phase-0
    metadata artifacts feed the lake's own calendar bootstrap; the Python
    engine never opens them directly (see :func:`_build_engine_run_spec`),
    so they are excluded regardless of resolution.
    """
    if artifact.artifact_kind != "time_series_bars":
        return False
    is_aggregated = artifact.trading_date is None
    return is_aggregated if resolution == "daily" else not is_aggregated


def _verify_bytes_on_disk(
    artifacts: Iterable[ArtifactRecord],
    *,
    lake_root: str,
    resolution: EngineResolution,
    symbol: str,
    start: date,
    end: date,
) -> None:
    """Refuse rather than hand a run bytes the catalog only believes exist.

    ``ensure_data``'s claim machinery checks the catalog, not the
    filesystem, once a row is already 'complete' — a reused artifact is
    never re-touched, so a catalog row can go on describing a file that
    volume loss, a restored-from-snapshot host, or a manual prune already
    removed. The Python engine's reader (``LeanMinuteDataReader`` /
    ``LeanDailyDataReader``) treats a missing day as an ordinary hole in the
    series, not an error — so without this check, a run silently trades on
    a series one or more sessions short of what the fingerprint it records
    claims to have materialized.

    Deliberately a cheap ``stat()`` (existence + size), not a re-hash: a
    full SHA-256 comparison against every reused artifact on every run
    would turn a stat-cost check into an I/O-bound one for artifacts that
    already cost nothing to reuse, precisely the case this codepath exists
    to keep cheap. Byte-exact verification against a corrupted-but-right-
    sized file is the observatory's and the backfill job's job (they can
    afford to walk every artifact off the request's critical path), not
    every run's.

    Only artifacts this run's own reader will open are checked (via
    :func:`_opened_by_this_run`) — a stale daily zip is not this function's
    business on a minute run, matching the coverage gate's own scoping.
    """
    for artifact in artifacts:
        if not _opened_by_this_run(artifact, resolution=resolution):
            continue
        path = Path(lake_root) / artifact.file_path
        try:
            actual_size = path.stat().st_size
        except OSError:
            raise LakeMaterializationError(
                f"the lake cannot serve {symbol} {start}..{end}: the catalog names "
                f"{artifact.file_path!r} as a complete artifact but it is not on disk "
                "(volume loss or a stale snapshot) — refusing rather than running on a "
                "gap the reader would silently treat as an ordinary missing day"
            ) from None
        if artifact.file_size_bytes is not None and actual_size != artifact.file_size_bytes:
            raise LakeMaterializationError(
                f"the lake cannot serve {symbol} {start}..{end}: {artifact.file_path!r} is "
                f"{actual_size} bytes on disk but the catalog recorded "
                f"{artifact.file_size_bytes} — refusing rather than running on bytes that "
                "have changed since the catalog last saw them"
            )


def materialize_engine_run(
    *,
    symbol: str,
    start: date,
    end: date,
    resolution: EngineResolution = "minute",
    price_adjustment_mode: PriceAdjustmentMode = "raw",
    requester: str | None = None,
) -> EngineRunMaterialization:
    """Put a backtest run's bars in the lake and report what it will read.

    Returns an :class:`EngineRunMaterialization` — the four facts a run has any
    business acting on — rather than the raw ``DataAvailabilityResult``. The
    gate below is the only place the failure list is judged, and handing that
    list onward would invite a second caller to judge it differently.

    Raises :class:`LakeMaterializationError` rather than returning a result the
    run would silently misread. Three cases:

    - Nothing materialized at all (``overall_status == "failed"``).
    - Partial coverage that withholds the artifact class this resolution
      reads. This is the gate, and it is not hypothetical: the derived daily
      artifact's data contract is keyed by the *set* of minute artifacts it
      aggregated, so a second run over a different window gets
      ``data_contract_mismatch`` on it — a partial result whose daily zip on
      disk is the previous window's. Proceeding would feed a daily-resolution
      run stale bars with a fingerprint that says everything was fine. (The
      deeper fix — having ``ensure_data`` re-aggregate instead of reporting a
      mismatch — is the integration slice's.)
    - Complete coverage per the catalog, but a reused artifact's file is not
      actually on disk at its recorded size (see :func:`_verify_bytes_on_disk`)
      — the catalog says "complete" for a row it has not re-touched since it
      last wrote it, so this is the only place a materialized-against-nothing
      gap gets caught before the reader silently treats it as an ordinary
      missing day.

    Failures that withhold nothing this run reads do not stop it; they come
    back as ``incomplete_summary`` so the caller can say so to the operator.

    **The fingerprint's scope — canonical statement, referenced elsewhere.**
    ``availability_hash`` is the lake's ``data_availability_hash``, and it
    covers a **superset** of what the Python engine opens: the Phase-0 metadata
    artifacts and the derived daily zip are in it whether or not this run's
    reader touches them. Treat it as "the lake state this run materialized
    against", not as a byte-exact receipt for the bars it consumed.
    """
    spec = _build_engine_run_spec(
        symbol=symbol,
        start=start,
        end=end,
        price_adjustment_mode=price_adjustment_mode,
        requester=requester,
    )
    result = _materialize_run_data_sync(spec)

    if result.overall_status == "failed":
        raise LakeMaterializationError(
            f"the lake could not materialize {symbol} {start}..{end}: {_describe_failures(result.failures)}"
        )

    withheld = [f for f in result.failures if _withholds_bars_the_run_reads(f, resolution=resolution)]
    if withheld:
        raise LakeMaterializationError(
            f"the lake has incomplete {resolution} coverage for {symbol} {start}..{end}: "
            f"{_describe_failures(withheld)} — refusing rather than running on bars "
            "that do not match the request"
        )

    _verify_bytes_on_disk(
        result.artifacts,
        lake_root=result.lean_data_root_path,
        resolution=resolution,
        symbol=symbol,
        start=start,
        end=end,
    )

    incomplete_summary = _describe_failures(result.failures) if result.failures else None
    if incomplete_summary:
        logger.warning(
            "data_lake.run_materialization: partial coverage for %s %s..%s (%s run) — %s",
            symbol,
            start,
            end,
            resolution,
            incomplete_summary,
        )
    logger.info(
        "data_lake.run_materialization: %s %s..%s fetched=%d reused=%d manifest=%s",
        symbol,
        start,
        end,
        result.fetched_artifact_count,
        result.reused_artifact_count,
        result.data_availability_hash,
    )
    return EngineRunMaterialization(
        availability_hash=result.data_availability_hash,
        fetched_artifact_count=result.fetched_artifact_count,
        reused_artifact_count=result.reused_artifact_count,
        incomplete_summary=incomplete_summary,
    )
