"""Run materialization: the seam where a backtest run asks the lake for bytes.

A backtest run needs LEAN-format bars on disk before the engine can read
them. Historically that was the policy store's ``ensure_range``, which
exported Polygon aggregates into a policy-keyed cache directory. With
``DATA_LAKE_ENABLED`` the same question is answered by the lake:
``ensure_data`` materializes exactly the missing artifacts, the catalog
arbitrates who fetches what, and every run leaves with the fingerprint of
the bytes it consumed.

This module is only the bridge. Callers want one call —
:func:`materialize_engine_run` — and it composes three things none of them
should have to solve for themselves:

1. **The spec.** What a Python-engine run actually needs from the lake —
   minute and daily trade bars, and nothing else (see
   :func:`build_engine_run_spec`).
2. **Contention.** Two runs wanting the same day is normal, not an error.
   The catalog hands the fetch to one of them; the other waits and takes
   the winner's bytes (see :func:`materialize_run_data`).
3. **The sync boundary.** Backtests run on worker threads with no event
   loop, and the catalog's connection pool is bound to the loop that
   created it (see :func:`materialize_run_data_sync`).

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Iterable
from datetime import date
from uuid import UUID, uuid4

from app.data_lake.ensure_data import ensure_data
from app.data_lake.types import ArtifactFailure, DataAvailabilityResult, DataRunSpec

logger = logging.getLogger(__name__)

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


def build_engine_run_spec(
    *,
    symbol: str,
    start: date,
    end: date,
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


async def materialize_run_data(
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
# ``catalog_client`` keeps one module-global asyncpg pool, and an asyncpg
# pool belongs to the event loop that created it. ``asyncio.run`` per call
# would therefore leave that global bound to a closed loop and break the
# next run; a process-wide lock around it would fix that by serializing
# every materialization, which is worse than what it replaces — the policy
# store's lock is per symbol, so two runs on different symbols never wait
# on each other today.
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


def materialize_run_data_sync(spec: DataRunSpec) -> DataAvailabilityResult:
    """Blocking :func:`materialize_run_data` for callers without an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "materialize_run_data_sync was called from a running event loop; await materialize_run_data instead"
        )

    future = asyncio.run_coroutine_threadsafe(materialize_run_data(spec), _materialization_loop())
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


def describe_failures(failures: Iterable[ArtifactFailure]) -> str:
    """One short ``kind/reason`` summary of what the lake could not produce."""
    return "; ".join(sorted({f"{f.artifact_kind}/{f.reason}" for f in failures}))


def _withholds_bars_the_run_reads(failure: ArtifactFailure, *, resolution: str) -> bool:
    """Does this failure hold back bars the run's reader will actually open?

    ``ArtifactFailure`` carries no resolution, but within ``time_series_bars``
    the spec makes the discriminator exact: per-day minute artifacts carry a
    ``trading_date``, and the per-symbol aggregated (daily) artifact does not.
    A minute-resolution run opens the former, a daily-resolution run the
    latter. Metadata failures mean the lake fell back to its hardcoded
    calendar — bad, and logged — but they withhold no bars.
    """
    if failure.artifact_kind != "time_series_bars":
        return False
    is_aggregated = failure.trading_date is None
    return is_aggregated if resolution == "daily" else not is_aggregated


def materialize_engine_run(
    *,
    symbol: str,
    start: date,
    end: date,
    resolution: str = "minute",
    requester: str | None = None,
) -> DataAvailabilityResult:
    """Put a backtest run's bars in the lake and report what it will read.

    Raises :class:`LakeMaterializationError` rather than returning a result the
    run would silently misread. Two cases:

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

    Failures that withhold nothing this run reads are logged and allowed
    through; the returned fingerprint then covers whatever did materialize.

    The fingerprint is the lake's ``data_availability_hash``, which covers a
    **superset** of what the Python engine opens: the Phase-0 metadata
    artifacts and the derived daily zip are in it whether or not this run's
    reader touches them. Treat it as "the lake state this run materialized
    against", not as a byte-exact receipt for the bars it consumed.
    """
    spec = build_engine_run_spec(symbol=symbol, start=start, end=end, requester=requester)
    result = materialize_run_data_sync(spec)

    if result.overall_status == "failed":
        raise LakeMaterializationError(
            f"the lake could not materialize {symbol} {start}..{end}: {describe_failures(result.failures)}"
        )

    withheld = [f for f in result.failures if _withholds_bars_the_run_reads(f, resolution=resolution)]
    if withheld:
        raise LakeMaterializationError(
            f"the lake has incomplete {resolution} coverage for {symbol} {start}..{end}: "
            f"{describe_failures(withheld)} — refusing rather than running on bars "
            "that do not match the request"
        )

    if result.failures:
        logger.warning(
            "data_lake.run_materialization: partial coverage for %s %s..%s (%s run) — %s",
            symbol,
            start,
            end,
            resolution,
            describe_failures(result.failures),
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
    return result
