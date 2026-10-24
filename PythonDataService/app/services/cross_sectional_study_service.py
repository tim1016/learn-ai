"""Cross-sectional study (Research Lab → Features → Cross-sectional): stock bars from admitted lake inputs (#3524).

The study's input contract. What it read before #3524 (the data-source census, #3491), and what it reads now, field by
field. Each ticker needs two kinds of input, and only one of them comes from the lake:

* **Stock bars.** Before: Polygon daily aggregates, fetched twice per ticker (once inside the IV build, which fetched the
  underlying itself when it was not handed bars, and once for the target). Now: one admitted set of daily rows per
  ticker, passed to both; the IV build has no fetch of its own. The rows are the lake's one-minute trade bars, admitted
  through :mod:`app.research.study_inputs` (the existing materializer and committed-byte admission; the UI is never a
  writer) and folded to one bar per New York session by :func:`app.research.cross_sectional_inputs.daily_rows`.
* **Option inputs.** Unchanged and still the provider's: expiries (``list_options_contracts`` as of each month's first
  session, cached per month), contracts (per expiry, cached for the run), daily option bars (5 threads), and the FRED
  risk-free rate. The lake holds no daily option bar and no vendor VWAP (its per-date listings and minute option bars
  are a different input), so none moves in this slice. Each is named in the result's ``data_source`` with the identity of what the provider answered
  (:mod:`app.research.options.option_input_ledger`); the provider does not archive its answers, so that identity cannot
  reproduce them and the block says so. Missing IV stays absent: no current IV is written into a past date.
* **Resolution.** Daily only. The picker offered minute and hour bars and the study ignored them; any other resolution is
  now refused with ``resolution_unsupported`` (#3527).
* **Adjustment.** Before: Polygon ``adjusted=True`` (split-only). Now: the lake's ``polygon_split_adjusted`` mode, named in
  the identity. Prices sit on the lake's 1/10,000 grid, each minute rounded half-up before the fold.
* **Session.** Before: whatever session Polygon's daily bar covers. The repo treats its close as the regular-session close
  (``dataset_service.fetch_rth_closes``); the census marks the rest unverified. Now: ``session_policy``, an identity field,
  default ``regular`` — the nearest to the old close, and the one whose close is the IV spot at the option prices' own time.
  ``extended`` folds 04:00-20:00 ET, as the lake's own daily rollup is built, so its close is the last after-hours print.
  The browser sends ``session: "rth"`` and the backend never read it; that label stays inert. The stock close sets the IV
  spot and the ATM strike, so the choice moves IVs and contract picks, not only the target. Which session the study should
  use is an owner decision (D02).
* **Warmup.** None, as before. IV rank 60/252 need 30/60 rows inside the window; the other features need 5-20.
* **Look-forward.** Inside the window only, as before: ``directional`` and ``abs_return`` look one session ahead,
  ``volatility`` five. The last rows have no target. Nothing is read past the window's end.
* **Window.** The scheduled sessions in ``[from_date, to_date]`` (canonical calendar). The lake holds completed sessions
  only, so a window that ends on an unfinished session is refused (``window_not_complete``); the provider path included
  the forming session's partial bar.
* **Bar timestamp.** The session open, ``int64 ms UTC``, where Polygon stamps 00:00 ET. Every downstream use takes the UTC
  date of the stamp, which is the same date for both.
* **Cost of a cold universe.** A session the lake lacks is one vendor call, per member: a year of ten symbols is about
  2,500 captures where the provider path made 10 daily calls. A held session costs nothing, and the captures are kept.
* **Cache and result.** Before: Redis ``cross_sectional``, keyed on the request fields, looked up before any fetch, 24 h,
  with a failed ticker's error row cached too. Now: a new namespace keyed on the identity, each member's admitted snapshot
  digest, the scientific inputs and the calculation version, looked up after admission (the digest needs the bytes).

The job around it:

1. **Admit.** Each requested member's daily stock bars are admitted from the lake (materializing what it lacks) before
   anything is computed. A member the lake cannot admit is excluded with its reason; when none can be, the job fails with a
   typed refusal and computes nothing.
2. **Look up.** The result cache is consulted *after* admission. The namespace is new, so an entry written when the provider
   supplied the bars can never be found, and none is relabelled.
3. **Compute.** :func:`app.research.batch_runner.run_cross_sectional_study` on the admitted rows.
4. **Report.** The result carries a ``data_source`` block that names every input and its source (it is *not* labelled "lake"
   as a whole) and a ``universe`` block that accounts for every requested member.

The ``universe`` block (T18). ``requested`` are the symbols asked for; ``evaluated`` the members in the statistics — the
study's existing inclusion policy counts valid members only, so that is also the ``denominator``; ``excluded`` holds every
other member exactly once with its stage, code and plain reason; ``degraded`` the members that are in the statistics but built
their IV history with provider requests unanswered. ``status`` is ``complete`` only when every requested member is evaluated and
none built its IV history with option requests unanswered; otherwise ``partial``. A ten-symbol study that evaluates nine says so.

Caching. A result is stored only when nothing in it could answer differently on a second attempt: no member excluded for a
retryable reason (a provider request that did not answer, a session another run is still capturing, an exception) and no
unanswered option request. A result with a failure in it is never served from cache (#3527). ``force`` skips the lookup and
recomputes. The option inputs are not part of the lookup key, because they are unknown until fetched; the key names the policy
(:data:`OPTION_INPUT_POLICY`) and the stored result carries the observation digests it saw.

Differences a run can show against the provider path are documented and measured in
``tests/research/test_cross_sectional_lake_vs_provider.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from app.jobs import cache as result_cache
from app.jobs.phases import friendly as friendly_phase
from app.jobs.progress import CancellationCheck, ProgressEmitter
from app.research.batch_runner import CrossSectionalReport, run_cross_sectional_study
from app.research.cross_sectional_inputs import (
    CrossSectionalIdentity,
    UniverseAdmission,
    admit_universe,
    cross_sectional_identity,
    refusal_code_for,
    stock_bars_block,
    sum_groups,
)
from app.research.options.option_input_ledger import combine
from app.research.study_inputs import AdjustmentMode, SessionPolicy, StudyInputRefusal
from app.services.polygon_client import PolygonClientService

logger = logging.getLogger(__name__)

#: Bump when the IC, stationarity, quantile, N_eff, binomial or graduation math changes, or when what the option
#: inputs are asked for changes: a cached result is the product of the code that made it.
CROSS_SECTIONAL_CALCULATION_VERSION = "cross-sectional-study/1"

#: The lake-sourced cache namespace. Entries written when the provider was the stock-bar source live under
#: ``cross_sectional`` and are never read by a lake request.
CACHE_NAMESPACE = "cross_sectional:lake:v1"

#: How the option inputs are obtained. They are asked of the provider live and are not archived, so they cannot be
#: part of a lookup key (they are unknown until fetched); the key names the policy and the result carries the
#: observation digests it actually saw.
OPTION_INPUT_POLICY = "polygon_rest_live/v1"

#: What "valid members only" means, in the words the result shows.
INCLUSION_POLICY = {
    "name": "valid_members_only",
    "description": (
        "Statistics (aggregate IC, N_eff, binomial test, stage) count only members whose per-ticker IC ran on usable "
        "stock bars and IV. Every other requested member is listed as excluded, with its reason."
    ),
}

_REQUESTER = "cross-sectional-study"

_MAX_LISTED_MEMBERS = 50


@dataclass(frozen=True)
class CrossSectionalParams:
    """The scientific request: what a run is asked to study."""

    feature_name: str
    symbols: tuple[str, ...]
    from_date: date
    to_date: date
    timespan: str
    multiplier: int
    target_type: str = "directional"
    session_policy: SessionPolicy = "regular"
    price_adjustment_mode: AdjustmentMode = "polygon_split_adjusted"


def cache_params(identity: CrossSectionalIdentity, admission: UniverseAdmission, params: CrossSectionalParams) -> dict[str, Any]:
    """The cache identity: data policy, each member's admitted bytes, the scientific inputs and the code that computes them."""
    return {
        "identity": identity.as_dict(),
        "members": {
            ticker: {"snapshot_digest": member.admitted.snapshot_digest, "data_root_id": str(member.admitted.data_root_id)}
            for ticker, member in sorted(admission.members.items())
        },
        "excluded_at_admission": {ticker: e.code for ticker, e in sorted(admission.excluded.items())},
        "feature_name": params.feature_name,
        "target_type": params.target_type,
        "option_input_policy": OPTION_INPUT_POLICY,
        "calculation_version": CROSS_SECTIONAL_CALCULATION_VERSION,
    }


def universe_record(requested: Sequence[str], rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Account for every requested member: evaluated, excluded with a reason, or evaluated on degraded IV inputs."""
    evaluated = [row["ticker"] for row in rows if row["validity"] == "valid"]
    excluded: list[dict[str, Any]] = []
    for row in rows:
        if row["validity"] == "valid":
            continue
        if row["exclusion"] is None:
            raise RuntimeError(f"{row['ticker']} is {row['validity']} but carries no exclusion")
        excluded.append(row["exclusion"])
    degraded = [
        {"ticker": row["ticker"], "failures_total": row["option_inputs"]["failures_total"]}
        for row in rows
        if row["validity"] == "valid" and row["option_inputs"] and row["option_inputs"]["failures_total"]
    ]
    return {
        "requested": list(requested),
        "requested_count": len(requested),
        "evaluated": evaluated,
        "evaluated_count": len(evaluated),
        "excluded": excluded[:_MAX_LISTED_MEMBERS],
        "excluded_count": len(excluded),
        "degraded": degraded[:_MAX_LISTED_MEMBERS],
        "denominator": len(evaluated),
        "inclusion_policy": INCLUSION_POLICY,
        "complete": len(evaluated) == len(requested) and not degraded,
    }


def is_cacheable(rows: Sequence[dict[str, Any]]) -> bool:
    """True when no member's outcome could differ on a second attempt: nothing retryable, no unanswered request."""
    for row in rows:
        exclusion = row["exclusion"]
        if exclusion is not None and exclusion["retryable"]:
            return False
        if row["option_inputs"] and row["option_inputs"]["failures_total"]:
            return False
    return True


def data_source_block(
    identity: CrossSectionalIdentity | None,
    admission: UniverseAdmission | None,
    report: CrossSectionalReport | None,
    *,
    cache_hit: bool = False,
    cached_at_ms: int | None = None,
) -> dict[str, Any]:
    """Where each input came from. The stock bars are the lake's; the option inputs are the provider's, named apart."""
    return {
        "kind": "composite",
        "calculation_version": CROSS_SECTIONAL_CALCULATION_VERSION,
        "stock_bars": stock_bars_block(identity, admission) if identity is not None and admission is not None else None,
        "option_inputs": {**combine(report.option_ledgers), "policy": OPTION_INPUT_POLICY} if report is not None else None,
        "cache": {"hit": cache_hit, "cached_at_ms": cached_at_ms},
    }


def serialize_cross_sectional_report(report: CrossSectionalReport) -> dict[str, Any]:
    """The scientific result: the report as the Frontend's snake-case mapper reads it.

    ``tickers_tested`` is kept as a legacy alias for the raw count so any older consumer keeps working; new
    consumers read ``tickers_tested_raw`` / ``tickers_valid`` / ``validity_summary`` and the ``universe`` block.
    """
    return {
        "success": True,
        "feature_name": report.feature_name,
        "target_type": report.target_type,
        "tickers_tested": report.tickers_tested_raw,
        "tickers_tested_raw": report.tickers_tested_raw,
        "tickers_valid": report.tickers_valid,
        "tickers_passed": report.tickers_passed,
        "pass_rate": report.pass_rate,
        "cross_sectional_consistent": report.cross_sectional_consistent,
        "aggregate_ic": report.aggregate_ic,
        "aggregate_ic_uniform": report.aggregate_ic_uniform,
        "aggregate_ic_ci": asdict(report.aggregate_ic_ci),
        "binomial_test": asdict(report.binomial_test),
        "n_eff_assets": report.n_eff_assets,
        "n_eff_assets_method": report.n_eff_assets_method,
        "validity_summary": asdict(report.validity_summary),
        "stage_info": asdict(report.stage_info),
        "ticker_results": report.ticker_results,
        "summary": report.summary,
    }


def _phase(emit: ProgressEmitter, cancel: CancellationCheck, phase_id: str, message: str | None = None) -> None:
    cancel.raise_if_cancelled()
    emit.phase(phase_id)
    emit.log(message if message is not None else friendly_phase("cross_sectional", phase_id))


def _coverage_line(admission: UniverseAdmission, requested: int) -> str:
    total = sum_groups(c.window for c in admission.coverage.values())
    return (
        f"Lake coverage: {len(admission.members)} of {requested} members admitted; {total.expected} member-sessions, "
        f"{total.reused} reused, {total.added} added, {total.missing} missing, {total.failed} failed"
    )


def run_cross_sectional_job(
    params: CrossSectionalParams,
    *,
    job_id: str,
    force: bool,
    emit: ProgressEmitter,
    cancel: CancellationCheck,
    polygon_client: PolygonClientService,
    now_ms: int | None = None,
) -> dict | None:
    """Admit the universe's stock bars, compute, and return the result — or answer a refusal or cache hit itself.

    Returns the result dict for the job runner to complete. Returns ``None`` when the job's terminal event is
    already emitted here: a refusal fails the job (typed code, plain reason) and parks the refusal payload with
    the per-member reasons as the job's result; a cache hit completes it flagged ``cached``.
    Blocking: runs on the job's worker thread.
    """
    try:
        identity = cross_sectional_identity(
            params.symbols,
            params.from_date,
            params.to_date,
            timespan=params.timespan,
            multiplier=params.multiplier,
            session_policy=params.session_policy,
            price_adjustment_mode=params.price_adjustment_mode,
        )
    except StudyInputRefusal as refusal:
        _refuse(emit, params, refusal.code, refusal.reason, identity=None, admission=None)
        return None
    return _run(params, identity, job_id=job_id, force=force, emit=emit, cancel=cancel, polygon_client=polygon_client, now_ms=now_ms)


def _refuse(
    emit: ProgressEmitter,
    params: CrossSectionalParams,
    code: str,
    reason: str,
    *,
    identity: CrossSectionalIdentity | None,
    admission: UniverseAdmission | None,
) -> None:
    logger.warning("cross-sectional study refused", extra={"action": "cross_sectional_refused", "code": code})
    emit.log(reason, level="warn")
    members = [e.as_dict() for e in admission.excluded.values()] if admission is not None else []
    emit.failed(
        code,
        reason,
        result={
            "success": False,
            "status": "refused",
            "feature_name": params.feature_name,
            "target_type": params.target_type,
            "error": reason,
            "refusal": {"code": code, "reason": reason, "members": members[:_MAX_LISTED_MEMBERS], "members_total": len(members)},
            "universe": {
                "requested": list(params.symbols),
                "requested_count": len(params.symbols),
                "evaluated": [],
                "evaluated_count": 0,
                "excluded": members[:_MAX_LISTED_MEMBERS],
                "excluded_count": len(members),
                "degraded": [],
                "denominator": 0,
                "inclusion_policy": INCLUSION_POLICY,
                "complete": False,
            },
            "data_source": data_source_block(identity, admission, None),
        },
    )


def _run(
    params: CrossSectionalParams,
    identity: CrossSectionalIdentity,
    *,
    job_id: str,
    force: bool,
    emit: ProgressEmitter,
    cancel: CancellationCheck,
    polygon_client: PolygonClientService,
    now_ms: int | None,
) -> dict | None:
    symbols = list(params.symbols)
    cancel.raise_if_cancelled()
    emit.phase("starting")
    emit.log(
        f"Starting cross-sectional study on {len(symbols)} ticker{'' if len(symbols) == 1 else 's'} "
        f"({', '.join(symbols[:5])}{'…' if len(symbols) > 5 else ''}) for feature '{params.feature_name}'"
    )
    _phase(
        emit,
        cancel,
        "admitting_data",
        f"Checking the lake for {len(symbols)} members' daily bars from {params.from_date.isoformat()} "
        f"to {params.to_date.isoformat()} ({params.session_policy} session)",
    )

    def on_member(index: int, total: int, ticker: str, outcome: str) -> None:
        emit.log(f"[{index + 1}/{total}] {ticker}: {outcome}")
        emit.progress(current=index + 1, total=total, unit="tickers", message=f"{ticker}: {outcome}")

    admission = admit_universe(
        identity, requester=_REQUESTER, now_ms=now_ms, check_cancelled=cancel.raise_if_cancelled, on_member=on_member
    )
    emit.log(_coverage_line(admission, len(symbols)))

    if not admission.members:
        excluded = list(admission.excluded.values())
        shown = " ".join(f"{e.ticker}: {e.reason}" for e in excluded[:3])
        more = f" And {len(excluded) - 3} more." if len(excluded) > 3 else ""
        _refuse(
            emit,
            params,
            refusal_code_for(admission.excluded),
            f"The lake cannot admit stock bars for any of the {len(symbols)} requested members. {shown}{more}",
            identity=identity,
            admission=admission,
        )
        return None

    key = cache_params(identity, admission, params)
    if not force:
        hit = result_cache.lookup(CACHE_NAMESPACE, key)
        if hit is not None:
            _, cached = hit
            cached_at = int(cached.get("cached_at", 0))
            stored = cached["result"]
            result = {
                **stored,
                # This run's admission and the cache's own account; the option inputs stay the ones the stored
                # run actually saw, because that is what its numbers were computed from.
                "data_source": {
                    **stored["data_source"],
                    "stock_bars": stock_bars_block(identity, admission),
                    "cache": {"hit": True, "cached_at_ms": cached_at},
                },
            }
            result_cache.serve_cached_result(
                job_id, "cross_sectional", {"result": result, "cached_at": cached_at}, announce_start=False
            )
            return None

    def on_phase(phase: str) -> None:
        cancel.raise_if_cancelled()
        emit.phase(phase)

    def on_log(message: str) -> None:
        emit.log(message)

    def on_progress(current: int, total: int, message: str) -> None:
        emit.progress(current=current, total=total, unit="tickers", message=message)

    def cancel_check() -> bool:
        # Raises JobCancelled when set; otherwise returns False so call sites can use it either as a poll or as a
        # sync barrier.
        cancel.raise_if_cancelled()
        return False

    report = run_cross_sectional_study(
        feature_name=params.feature_name,
        tickers=symbols,
        start_date=params.from_date.isoformat(),
        end_date=params.to_date.isoformat(),
        polygon_client=polygon_client,
        stock_bars=admission.stock_bars(),
        target_type=params.target_type,
        excluded=admission.excluded,
        on_phase=on_phase,
        on_log=on_log,
        on_progress=on_progress,
        cancel_check=cancel_check,
    )

    cancel.raise_if_cancelled()
    _phase(emit, cancel, "completed")
    emit.log(report.summary)

    universe = universe_record(symbols, report.ticker_results)
    result = {
        **serialize_cross_sectional_report(report),
        "status": "complete" if universe["complete"] else "partial",
        "universe": universe,
        "data_source": data_source_block(identity, admission, report),
    }
    if is_cacheable(report.ticker_results):
        result_cache.store(CACHE_NAMESPACE, key, result)
    else:
        emit.log("This result holds a failed request or a member that could not be evaluated, so it is not cached.", level="warn")
    return result
