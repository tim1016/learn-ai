"""Cross-sectional study (Research Lab → Features → Cross-sectional): stock bars from admitted lake inputs (#3524).

The study's input contract. Each requested member needs two kinds of input, and only one of them comes from the lake:

* **Stock bars.** One admitted set of daily rows per member, passed to both the IV build (spot and ATM strike) and the
  target; neither fetches the underlying itself. The rows are the lake's one-minute trade bars, admitted through
  :mod:`app.research.study_inputs` (the existing materializer and committed-byte admission; the UI is never a writer)
  and folded to one bar per New York session by :func:`app.research.cross_sectional_inputs.daily_rows`.
* **Option inputs.** The provider's, asked live: expiries (``list_options_contracts`` as of each month's first
  session, cached per month), contracts (per expiry, cached for the run), daily option bars (5 threads) and the FRED
  risk-free rate. The lake holds none of them. Each is named in the result's ``data_source`` with the identity of what
  the provider answered (:mod:`app.research.options.option_input_ledger`); the provider does not archive its answers,
  so that identity cannot reproduce them and the block says so. Missing IV stays absent: no current IV is written
  into a past date.
* **Resolution.** Daily only; any other resolution is refused with ``resolution_unsupported`` before the lake is touched.
* **Adjustment.** The lake's ``polygon_split_adjusted`` mode, named in the identity. Prices sit on the lake's 1/10,000
  grid, each minute rounded half-up before the fold: a change to the input data, not float rounding.
* **Session.** ``session_policy`` is an identity field, set from the request's ``session`` (``rth`` is ``regular``, the
  default; ``extended`` folds 04:00-20:00 ET). The stock close sets the IV spot and the ATM strike, so the policy moves
  IVs and contract picks, not only the target. Which policy the study should use is an owner decision (D02), and
  whether the regular fold equals Polygon's own daily bar is **unmeasured**: no real daily payload is held in the repo
  (see ``tests/research/test_cross_sectional_lake_vs_provider.py``).
* **Warmup.** None. IV rank 60/252 need 30/60 rows inside the window; the other features need 5-20.
* **Look-forward.** Inside the window only: ``directional`` and ``abs_return`` look one session ahead, ``volatility``
  five. The last rows have no target. Nothing is read past the window's end.
* **Window.** The scheduled sessions in ``[from_date, to_date]`` (canonical calendar). The lake holds completed
  sessions only, so a window that ends on an unfinished session is refused (``window_not_complete``).
* **Bar timestamp.** The session open, ``int64 ms UTC``. Every downstream use takes the UTC date of the stamp.

The job around it:

1. **Admit.** Each member's daily stock bars are admitted (materializing what the lake lacks) before anything is
   computed. A member the lake cannot admit is excluded with its reason; when none can be, the job fails with a typed
   refusal and computes nothing.
2. **Look up.** The result cache is consulted *after* admission, under a namespace of its own: the key holds the
   identity, each member's admitted snapshot digest and data root, the exclusions at admission, the feature, the
   target, the option-input policy, whether a FRED key is configured, and the calculation version. ``force`` skips it.
3. **Compute.** :func:`app.research.batch_runner.run_cross_sectional_study` on the admitted rows.
4. **Report.** A ``data_source`` block naming every input and its source (the stock bars are ``lake``, the option
   inputs ``provider``; the result as a whole is ``composite``) and a ``universe`` block accounting for every member.
   A study in which no member is evaluated fails with ``no_member_evaluated`` and each member's reason.

Partial universes (T18). ``requested`` are the symbols asked for; ``evaluated`` the members in the statistics, which
is also the ``denominator`` of every one of them; ``excluded`` every other member exactly once with its stage, code
and plain reason; ``degraded`` the evaluated members whose option inputs were short of what was asked (a provider
request unanswered, or a risk-free rate that is the default only because FRED did not answer). ``status`` is
``complete`` only when every requested member is evaluated and none is degraded; otherwise ``partial``.

Caching. A result is stored only when nothing in it could answer differently on a second attempt: no member excluded for
a retryable reason (a provider request that did not answer, a session another run is still capturing, an exception) and
no degraded member. A result with a failure in it is never served from cache (#3527). The option inputs are not part of
the lookup key, because they are unknown until fetched; the key names the policy (:data:`OPTION_INPUT_POLICY`) and the
stored result carries the observation digests it saw.

Differences a run can show against the old provider path are measured in
``tests/research/test_cross_sectional_lake_vs_provider.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date
from typing import Any

from app.jobs import cache as result_cache
from app.jobs.progress import CancellationCheck, ProgressEmitter
from app.research.batch_runner import CrossSectionalReport, run_cross_sectional_study
from app.research.cross_sectional_inputs import (
    CrossSectionalIdentity,
    UniverseAdmission,
    admit_universe,
    cross_sectional_identity,
    stock_bars_block,
    universe_coverage,
    universe_refusal,
)
from app.research.lake_study import announce_phase, coverage_line, hooks_for
from app.research.options.option_input_ledger import combine
from app.research.study_inputs import AdjustmentMode, SessionPolicy, StudyInputRefusal
from app.research.study_universe import (
    MAX_LISTED_MEMBERS,
    NO_MEMBER_EVALUATED,
    members_summary,
    universe_of_rows,
    universe_record,
)
from app.services.fred_service import fred_key_configured
from app.services.polygon_client import PolygonClientService

logger = logging.getLogger(__name__)

#: Bump when the IC, stationarity, quantile, N_eff, binomial or graduation math changes, when the IV build changes
#: (``app/research/options/iv_builder.py``, ``contract_finder.py``, the solver ``app/volatility/solver.py`` or the
#: variance interpolation it calls), or when what the option inputs are asked for changes: a cached result is the
#: product of the code that made it, and the cache key is the only thing that tells a new run apart from an old one.
CROSS_SECTIONAL_CALCULATION_VERSION = "cross-sectional-study/1"

#: The lake-sourced cache namespace. Entries written when the provider was the stock-bar source live under
#: ``cross_sectional`` and are never read by a lake request.
CACHE_NAMESPACE = "cross_sectional:lake:v1"

#: How the option inputs are obtained. They are asked of the provider live and are not archived, so they cannot be
#: part of a lookup key (they are unknown until fetched); the key names the policy and the result carries the
#: observation digests it actually saw.
OPTION_INPUT_POLICY = "polygon_rest_live/v1"

_REQUESTER = "cross-sectional-study"
_JOB_TYPE = "cross_sectional"


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
    """The cache identity: data policy, each member's admitted bytes, the scientific inputs and the code that computes them.

    ``fred_key_configured`` is part of it because a deployment with no key prices every day at the one default rate,
    which is a stable answer, while one with a key prices at FRED's: the same request is two different studies.
    """
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
        "fred_key_configured": fred_key_configured(),
        "calculation_version": CROSS_SECTIONAL_CALCULATION_VERSION,
    }


def is_cacheable(rows: Sequence[dict[str, Any]]) -> bool:
    """True when no member's outcome could differ on a second attempt: nothing retryable, nothing degraded."""
    for row in rows:
        exclusion = row["exclusion"]
        if exclusion is not None and exclusion["retryable"]:
            return False
        if row["option_inputs"] and row["option_inputs"]["degraded"]:
            return False
    return True


def _stock_bars(
    identity: CrossSectionalIdentity, admission: UniverseAdmission, *, cache_hit: bool = False, cached_at_ms: int | None = None
) -> dict[str, Any]:
    return stock_bars_block(
        identity, admission, calculation_version=CROSS_SECTIONAL_CALCULATION_VERSION, cache_hit=cache_hit, cached_at_ms=cached_at_ms
    )


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
        "stock_bars": (
            _stock_bars(identity, admission, cache_hit=cache_hit, cached_at_ms=cached_at_ms)
            if identity is not None and admission is not None
            else None
        ),
        "option_inputs": {**combine(report.option_ledgers), "policy": OPTION_INPUT_POLICY} if report is not None else None,
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
    already emitted here: a refusal, or a study in which no member was evaluated, fails the job (typed code, plain
    reason) and parks its payload with the per-member reasons as the job's result; a cache hit completes it flagged
    ``cached``. Blocking: runs on the job's worker thread.
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
        _refuse(emit, params, refusal, identity=None, admission=None)
        return None
    return _run(
        params, identity, job_id=job_id, force=force, emit=emit, cancel=cancel, polygon_client=polygon_client, now_ms=now_ms
    )


def _fail(
    emit: ProgressEmitter,
    params: CrossSectionalParams,
    *,
    code: str,
    reason: str,
    status: str,
    universe: dict[str, Any],
    data_source: dict[str, Any],
    refusal: dict[str, Any] | None = None,
) -> None:
    """End the job with a typed code and park the payload that says why, so the page can show every member's reason."""
    logger.warning("cross-sectional study ended without a result", extra={"action": "cross_sectional_failed", "code": code})
    emit.log(reason, level="warn")
    payload: dict[str, Any] = {
        "success": False,
        "status": status,
        "feature_name": params.feature_name,
        "target_type": params.target_type,
        "error": reason,
        "universe": universe,
        "data_source": data_source,
    }
    if refusal is not None:
        payload["refusal"] = refusal
    emit.failed(code, reason, result=payload)


def _refuse(
    emit: ProgressEmitter,
    params: CrossSectionalParams,
    refusal: StudyInputRefusal,
    *,
    identity: CrossSectionalIdentity | None,
    admission: UniverseAdmission | None,
) -> None:
    """End the job because the lake could not supply the stock bars: the shared refusal shape, plus each member's reason."""
    members = [e.as_dict() for e in admission.excluded.values()] if admission is not None else []
    _fail(
        emit,
        params,
        code=refusal.code,
        reason=refusal.reason,
        status="refused",
        universe=universe_record(params.symbols, evaluated=[], excluded=members, degraded=[]),
        data_source=data_source_block(identity, admission, None),
        refusal={**refusal.as_dict(), "members": members[:MAX_LISTED_MEMBERS], "members_total": len(members)},
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
    # The job's own check is throttled for the hot loops inside the math. The run's boundaries (each phase, each
    # session admission reads, each member) are far apart in time, so each consults the cancel flag and slides the
    # job's lease. The shared capture itself is never interrupted: another reader may be waiting on it.
    at_boundary = replace(cancel, check_every_n=1)
    announce_phase(
        _JOB_TYPE,
        emit,
        at_boundary,
        "starting",
        f"Starting cross-sectional study on {len(symbols)} ticker{'' if len(symbols) == 1 else 's'} "
        f"({', '.join(symbols[:5])}{'…' if len(symbols) > 5 else ''}) for feature '{params.feature_name}', "
        f"target '{params.target_type}', {params.from_date.isoformat()} to {params.to_date.isoformat()}",
    )
    announce_phase(
        _JOB_TYPE,
        emit,
        at_boundary,
        "admitting_data",
        f"Checking the lake for {len(symbols)} members' daily bars ({params.session_policy} session)",
    )

    def on_member_start(index: int, total: int, ticker: str) -> None:
        emit.progress(current=index, total=total, unit="members admitted", message=f"Admitting {ticker}")

    def on_member(index: int, total: int, ticker: str, outcome: str) -> None:
        emit.log(f"[{index + 1}/{total}] {ticker}: {outcome}")
        emit.progress(current=index + 1, total=total, unit="members admitted", message=f"{ticker}: {outcome}")

    admission = admit_universe(
        identity,
        requester=_REQUESTER,
        now_ms=now_ms,
        check_cancelled=at_boundary.raise_if_cancelled,
        on_member_start=on_member_start,
        on_member=on_member,
    )
    emit.log(f"{len(admission.members)} of {len(symbols)} members admitted")
    emit.log(coverage_line(universe_coverage(admission)))

    if not admission.members:
        _refuse(emit, params, universe_refusal(identity, admission), identity=identity, admission=admission)
        return None

    key = cache_params(identity, admission, params)
    if not force:
        hit = result_cache.lookup(CACHE_NAMESPACE, key)
        if hit is not None:
            _, cached = hit
            cached_at = cached.get("cached_at")
            stored = cached["result"]
            result = {
                **stored,
                # This run's admission and the cache's own account; the option inputs stay the ones the stored
                # run actually saw, because that is what its numbers were computed from. A time the cache did not
                # record stays unknown, not zero.
                "data_source": {
                    **stored["data_source"],
                    "stock_bars": _stock_bars(
                        identity, admission, cache_hit=True, cached_at_ms=int(cached_at) if cached_at is not None else None
                    ),
                },
            }
            result_cache.serve_cached_result(
                job_id, _JOB_TYPE, {"result": result, "cached_at": cached_at}, announce_start=False
            )
            return None

    hooks = hooks_for(emit, cancel)
    report = run_cross_sectional_study(
        feature_name=params.feature_name,
        tickers=symbols,
        start_date=params.from_date.isoformat(),
        end_date=params.to_date.isoformat(),
        polygon_client=polygon_client,
        stock_bars=admission.stock_bars(),
        target_type=params.target_type,
        excluded=admission.excluded,
        on_phase=hooks.on_phase,
        on_log=hooks.on_log,
        on_progress=lambda current, total, message: hooks.on_progress(current, total, unit="tickers", message=message),
        cancel_check=hooks.cancel_check,
    )

    universe = universe_of_rows(symbols, report.ticker_results)
    if universe["evaluated_count"] == 0:
        reasons = members_summary([(row["ticker"], row["error"]) for row in report.ticker_results])
        _fail(
            emit,
            params,
            code=NO_MEMBER_EVALUATED,
            reason=f"None of the {len(symbols)} requested members could be evaluated, so there is no statistic to report. {reasons}",
            status="not_evaluated",
            universe=universe,
            data_source=data_source_block(identity, admission, report),
        )
        return None

    announce_phase(_JOB_TYPE, emit, at_boundary, "completed")
    emit.log(report.summary)
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
