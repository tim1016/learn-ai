"""Orchestrator: stage → launch → write manifest → return.

This is the single seam between the FastAPI router (transport) and
the launcher (process boundary). Keeping the staging + manifest +
launcher-call sequence here means:

- the router stays request-shape only (Pydantic, HTTPException);
- the manifest is always written, so every run is reproducibility
  evidence;
- a Phase 3+ change to "accept arbitrary algorithm source" only has to
  touch the staging step here, not the router.

Phase 2a constraints (per ``docs/architecture/lean-sidecar-lab.md``):

- no caller-supplied algorithm source — Phase 3 is the gating phase
  before that;
- trusted sample only; date range + starting cash are the only
  caller-tunable knobs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from app.config import settings
from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.config import (
    DEFAULT_ARTIFACTS_ROOT,
    DEFAULT_RUN_LIMITS,
    PINNED_LEAN_IMAGE_DIGEST,
)
from app.lean_sidecar.launcher.models import LaunchRequest, LaunchResponse
from app.lean_sidecar.launcher_client import post_launch
from app.lean_sidecar.lean_config import LeanConfig
from app.lean_sidecar.manifest import (
    MANIFEST_SCHEMA_VERSION,
    P2_5_DATE_SEMANTICS_NOTE,
    BrokeragePolicy,
    DataPolicy,
    RunManifest,
    StagedDataManifest,
    WindowMs,
    hash_staged_files,
    sha256_file,
    sha256_text,
    write_manifest,
)
from app.lean_sidecar.normalized_parser import (
    NORMALIZED_PARSER_VERSION,
    NormalizedParserError,
    NormalizedResult,
    parse_workspace,
    write_normalized_result,
)
from app.lean_sidecar.staging import (
    stage_algorithm_source,
    stage_daily_bars,
    stage_empty_corporate_action_dirs,
    stage_lean_config,
    stage_lean_metadata_from_image,
    stage_minute_bars,
    stage_quote_bars,
)
from app.lean_sidecar.trusted_samples.buy_and_hold import BUY_AND_HOLD_SOURCE
from app.lean_sidecar.trusted_samples.buy_and_hold_reconciliation import (
    BUY_AND_HOLD_RECONCILIATION_SOURCE,
)
from app.lean_sidecar.trusted_samples.deployment_validation import (
    DEPLOYMENT_VALIDATION_SOURCE,
)
from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE
from app.lean_sidecar.workspace import Workspace, resolve_workspace
from app.services.lean_sidecar_persistence import (
    _algorithm_name_for_run,
    build_persist_payload,
    persist_via_dotnet,
)
from app.utils.timestamps import now_ms_utc

# Phase 5b — selector for which trusted-sample source the orchestrator
# stages when the caller does not provide their own ``algorithm_source``.
# "trusted_default" keeps Phase 1's LEAN-default-brokerage behavior
# (backwards-compatible default); "reconciliation" pins IBKR brokerage
# explicitly so the Phase 5a fee reconciler returns a clean report.
TrustedTemplate = Literal["trusted_default", "reconciliation", "ema_crossover", "deployment_validation"]

# Maps the template selector to the manifest's ``brokerage_policy``
# enum so a reader of the manifest can tell at a glance which
# brokerage the sample's source actually pinned.
_BROKERAGE_POLICY_FOR_TEMPLATE: dict[TrustedTemplate, BrokeragePolicy] = {
    "trusted_default": "algorithm_default",
    "reconciliation": "interactive_brokers",
    "ema_crossover": "algorithm_default",
    "deployment_validation": "algorithm_default",
}

_SOURCE_FOR_TEMPLATE: dict[TrustedTemplate, str] = {
    "trusted_default": BUY_AND_HOLD_SOURCE,
    "reconciliation": BUY_AND_HOLD_RECONCILIATION_SOURCE,
    "ema_crossover": EMA_CROSSOVER_SOURCE,
    "deployment_validation": DEPLOYMENT_VALIDATION_SOURCE,
}

logger = logging.getLogger(__name__)

# Phase 2a uses the same in-process launcher version label as Phase 1.
# When the launcher gains its own deployable image this becomes its
# image digest.
LAUNCHER_VERSION_HASH = sha256_text("phase-1-spike-0")


class LeanSidecarServiceError(RuntimeError):
    """Raised when the orchestrator cannot fulfill a run request.

    Distinct from :class:`app.lean_sidecar.launcher_client.LauncherClientError`
    so the router can branch on "data plane couldn't even stage" vs
    "launcher rejected our request" vs "container actually ran".
    """


class RunIdAlreadyUsedError(LeanSidecarServiceError):
    """Raised when the caller submits a run with a ``run_id`` that
    already has an existing workspace on disk.

    Reusing a ``run_id`` would let the new run inherit stale artifacts
    (``output/log.txt``, ``normalized/*-summary.json``,
    ``manifest.json``, the LEAN ObjectStore) from the previous run,
    contaminating downstream parsers and producing a manifest that
    claims fresh-bar consumption while reading prior LEAN output.
    Server-side uniqueness is the simplest enforcement — fresh slug
    per run.

    Maps to HTTP 409 ``run_id_already_used`` in the router.
    """


@dataclass(frozen=True, slots=True)
class TrustedRunRequest:
    """LEAN sidecar run input — PR B canonical shape.

    Top-level ``symbol``/``bar_minutes``/``session``/``adjustment`` are gone;
    they live inside ``data_policy``. The router (``TrustedRunRequestModel``)
    accepts both shapes for one cycle and converts before constructing
    this dataclass.

    Phase 4c added the optional ``algorithm_source`` field. When provided,
    the orchestrator stages it as ``workspace/project/main.py`` instead of
    the bundled trusted sample; when ``None`` (default), the trusted sample
    selected by ``template`` is used. The Phase 1c sandbox shape
    (``--read-only``, ``--user=<non-root>``, ``--cap-drop=ALL``,
    ``--network=none``, workspace-only mount) is what makes accepting
    arbitrary source safe.

    ``start_ms_utc`` and ``end_ms_utc`` are int64 ms UTC per the repo's
    timestamp rigor rule. They are converted to ``date`` *inside this
    module* (under the boundary) so the LEAN config's ISO-string
    parameter values stay consistent with what the algorithm reads via
    ``GetParameter``.
    """

    run_id: str
    starting_cash: float
    start_ms_utc: int
    end_ms_utc: int
    data_policy: DataPolicy
    # Phase 4c — None means "use the bundled trusted sample selected
    # by ``template`` below". When provided, must be valid Python
    # source defining a ``MyAlgorithm`` class (LeanConfig.
    # algorithm_type_name's default).
    algorithm_source: str | None = None
    # Phase 5b — which trusted sample to stage when ``algorithm_source``
    # is None. "trusted_default" keeps Phase 1's LEAN-default-brokerage
    # behavior (backwards-compatible default for existing callers);
    # "reconciliation" pins IBKR brokerage explicitly so the Phase 5a
    # fee reconciler can return a clean report. Ignored when the
    # caller supplies their own ``algorithm_source`` — operator-pasted
    # source picks its own brokerage via SetBrokerageModel.
    template: TrustedTemplate = "trusted_default"

    @property
    def symbol(self) -> str:
        """Symbol carried by ``data_policy.symbol``.

        Kept as a property so the orchestrator body that grew up reading
        ``request.symbol`` works unchanged after the PR B refactor.
        """
        return self.data_policy.symbol

    @property
    def start_date(self) -> date:
        """First trading date in the window — NY-local date of ``start_ms_utc``.

        Per the P2.5 contract, ``start_ms_utc`` is the 09:30 ET
        session-open millisecond, so the NY-local date is the
        first trading day. The validator in the router has already
        confirmed this is a trading day (not weekend or holiday).
        """
        return datetime.fromtimestamp(self.start_ms_utc / 1000, tz=UTC).astimezone(_ET).date()

    @property
    def end_date(self) -> date:
        """Last trading date in the window — derived from the
        half-open ``end_ms_utc``.

        Per the P2.5 contract, ``end_ms_utc`` is 09:30 ET of the
        NEXT trading day after the window's last full session. We
        derive ``end_date`` by walking back from that exclusive-end
        date until we land on a trading day.
        """
        from app.lean_sidecar.trading_calendar import is_trading_day

        exclusive_end = datetime.fromtimestamp(self.end_ms_utc / 1000, tz=UTC).astimezone(_ET).date()
        d = exclusive_end - timedelta(days=1)
        # Validator guarantees a trading day exists between start and
        # exclusive end; this loop terminates.
        while not is_trading_day(d):
            d -= timedelta(days=1)
        return d


@dataclass(frozen=True, slots=True)
class TrustedRunResult:
    """What the router serializes back to the caller."""

    run_id: str
    is_clean: bool
    exit_code: int
    duration_ms: int
    timed_out: bool
    lean_errors: dict[str, list[str]]
    log_tail: str
    manifest_path: Path
    workspace_root: Path
    observations_path: Path
    lean_log_path: Path
    # Phase 3a — the normalized LEAN result (Pydantic-typed). ``None``
    # when the run never produced output (launcher rejected, container
    # died before write); the router surfaces that to the caller so
    # the inspection endpoint can 404 deterministically.
    normalized_path: Path | None
    normalized: NormalizedResult | None
    # Task 1.10 — the StrategyExecution.Id assigned by the .NET backend
    # after persisting this run. ``None`` when persistence failed or was
    # skipped (e.g., launcher rejected before a result was produced).
    strategy_execution_id: int | None = None


_ET = ZoneInfo("America/New_York")


def _date_to_ms_utc(d: date) -> int:
    """Convert an ET-resolved trading date's midnight UTC to int64 ms.

    The trusted sample's window keys are *trading dates*, not wall
    clocks; treating their midnight UTC as the boundary is sufficient
    for the manifest's window fields. Reconciliation-grade runs use the
    exchange-aligned millisecond boundaries from the LEAN result
    artifacts (Phase 5 work).
    """
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


def _generate_synthetic_bars(
    symbol: str,
    trading_date: date,
    *,
    count: int = 30,
    open_price: float = 100.00,
    increment: float = 0.01,
) -> list[TradeBar]:
    """Build a deterministic minute-bar series starting 09:30 ET.

    Phase 2a does not source Polygon data yet — the trusted sample
    has always run against synthetic deci-cent-clean bars precisely
    so the data-folder fidelity claim is intentional, not coincidental.
    Phase 5 reconciliation will swap this for Polygon-sourced bars.
    """
    market_open = datetime(trading_date.year, trading_date.month, trading_date.day, 9, 30, tzinfo=_ET)
    bars: list[TradeBar] = []
    for i in range(count):
        start = market_open + timedelta(minutes=i)
        close = open_price + (i * increment)
        bars.append(
            TradeBar(
                symbol=symbol,
                time=start,
                end_time=start + timedelta(minutes=1),
                open=Decimal(str(close - increment / 2)),
                high=Decimal(str(close + increment / 2)),
                low=Decimal(str(close - increment)),
                close=Decimal(str(close)),
                volume=1000 + i,
            )
        )
    return bars


def _aggregate_daily_bar(symbol: str, minute_bars: list[TradeBar]) -> TradeBar:
    """Collapse a day's minute bars into a single OHLCV daily bar.

    Real LEAN-format daily bars are open-of-first / high-max /
    low-min / close-of-last / sum-of-volume across the trading day —
    not "the last minute's OHLCV". A copied minute bar would mislead
    LEAN's daily-resolution paths (warmup, benchmark) into seeing a
    1-minute price range as the day's high/low. The benchmark equity
    curve is what consumes this in Phase 2a; reconciliation-grade
    runs in Phase 5 stage real daily data and skip this aggregator.
    """
    if not minute_bars:
        raise LeanSidecarServiceError(f"no minute bars to aggregate for {symbol}")
    first = minute_bars[0]
    last = minute_bars[-1]
    # ``time`` for the daily bar is session midnight in ET so the
    # writer (lean_format.write_lean_daily_zip) stamps the right
    # ``YYYYMMDD 00:00`` timestamp.
    session_date = first.time.astimezone(_ET).date()
    session_midnight = datetime(session_date.year, session_date.month, session_date.day, tzinfo=_ET)
    return TradeBar(
        symbol=symbol,
        time=session_midnight,
        end_time=session_midnight + timedelta(days=1),
        open=first.open,
        high=max(b.high for b in minute_bars),
        low=min(b.low for b in minute_bars),
        close=last.close,
        volume=sum(b.volume for b in minute_bars),
    )


def _count_bars_consumed(workspace: Workspace, symbol: str) -> dict[str, int]:
    """Phase 5e: per-symbol bar count parsed from observations.csv.

    The trusted sample (and any user algorithm following its
    convention) appends one row per received bar to
    ``<workspace>/output/storage/observations.csv`` with a leading
    ``ms_utc,close`` header. The count is (line count - 1) — the
    minus-one is for the header. Empty / missing / unreadable files
    return ``{}`` rather than raising: a user algorithm that doesn't
    write observations.csv still gets a successful run, the manifest
    just records "no bar-consumption evidence" honestly.

    Multi-symbol algorithms are out of scope here — the trusted
    sample is single-symbol and per-symbol tagging would need a
    schema change to observations.csv (Phase 5f+ if it lands).
    """
    obs_path = workspace.object_store_dir / "observations.csv"
    if not obs_path.exists():
        return {}
    try:
        text = obs_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("could not read observations.csv at %s: %s", obs_path, e)
        return {}
    # Count non-empty data rows (skip header + any trailing blank).
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) <= 1:
        # Header only (or fully empty) — no bars consumed.
        return {}
    return {symbol.upper(): len(lines) - 1}


def _staged_window_from_dates(trading_dates: list[date]) -> WindowMs | None:
    """Phase 5d: build the staged-data window in int64 ms UTC from the
    list of trading dates the orchestrator staged.

    The window envelope is [first_date 00:00 ET, (last_date + 1) 00:00 ET)
    — the ET-midnight-to-next-ET-midnight bracket that contains every
    bar in the staged zips. Reconciliation readers compare this against
    the requested window to surface "we asked for N days, only got M"
    cases.

    Returns ``None`` when the date list is empty so the manifest's
    ``staged_data_window_ms`` stays unset rather than carrying a
    zero-length window that would falsely claim staging happened.
    """
    if not trading_dates:
        return None
    first = trading_dates[0]
    last = trading_dates[-1]
    # ET midnight is the canonical reference — independent of DST UTC
    # offset. Same convention used by the per-bar ms encoding.
    start_et = datetime(first.year, first.month, first.day, tzinfo=_ET)
    end_et = datetime(last.year, last.month, last.day, tzinfo=_ET) + timedelta(days=1)
    start_ms = int(start_et.timestamp() * 1000)
    end_ms = int(end_et.timestamp() * 1000)
    return WindowMs(start_ms=start_ms, end_ms=end_ms)


def _runtime_polygon_adjustment(data_policy: DataPolicy) -> Literal["raw"]:
    """Return the ``adjustment`` kwarg to pass to ``fetch_canonical_minute_bars``.

    PR B P1 (review feedback): ``data_policy.adjusted`` records the
    *staging-pipeline INTENT* (spec § 4.4) but is **not** mapped to a
    Polygon adjustment mode at runtime. ``fetch_canonical_minute_bars``
    and the bundled trusted templates only accept ``"raw"`` today, so
    emitting ``"adjusted"`` here would 500 every PR-B-default request
    (``adjusted=True`` is the new-shape default).

    Until the pre-adjusted staging pipeline lands (Phase 2+) and the
    downstream fetcher / templates learn to consume adjusted bars,
    the runtime adjustment is pinned to ``"raw"``. The vocabulary-
    consistency check in :func:`_assert_adjustment_vocabulary_consistent`
    still allows ``(adjusted=True, "Raw")`` per the PR B matrix; this
    function just makes the pin explicit and unit-testable.
    """
    # NOTE: parameter intentionally accepted (not unused) so the
    # signature documents the intent — when staging widens, this
    # function is the single seam to update.
    del data_policy
    return "raw"


def _assert_adjustment_vocabulary_consistent(
    *,
    adjusted: bool,
    data_normalization_mode: str,
) -> None:
    """Enforce the PR B adjustment-vocabulary matrix:

      (adjusted=False, "Raw")       -> accept (PR A's existing case: raw -> raw)
      (adjusted=True,  "Raw")       -> accept (PR B: pre-adjusted staging; LEAN reads as Raw)
      (adjusted=False, "Adjusted")  -> reject (LEAN would adjust unadjusted Polygon data)
      (adjusted=True,  "Adjusted")  -> reject (double-adjustment)

    The ``adjusted`` flag is the staging-pipeline policy, not LEAN's runtime
    normalization mode. See docs/superpowers/specs/2026-05-19-pr-b-engine-lab-unified-design.md § 4.4.
    """
    if data_normalization_mode == "Adjusted":
        raise LeanSidecarServiceError(
            f"adjustment_vocabulary_mismatch: data_normalization_mode='Adjusted' is "
            f"never valid in PR B's pre-adjusted-staging contract; got adjusted={adjusted}"
        )
    if data_normalization_mode != "Raw":
        raise LeanSidecarServiceError(
            f"adjustment_vocabulary_mismatch: unsupported data_normalization_mode={data_normalization_mode!r}; "
            "valid: 'Raw'"
        )


def _iter_trading_dates(start: date, end: date) -> list[date]:
    """Inclusive trading-date sequence using the NYSE calendar.

    P2.5: routes through ``trading_calendar.is_trading_day`` so the
    validator and staging consult the SAME calendar — the bug class
    "staging stages a date the validator already accepted as
    blocked" cannot exist. Weekends and holidays in between
    endpoints are allowed and silently skipped. Early-close half-days
    are trading sessions, so they are included when NYSE publishes
    them in the schedule.
    """
    from app.lean_sidecar.trading_calendar import is_trading_day

    out: list[date] = []
    current = start
    one = timedelta(days=1)
    while current <= end:
        if is_trading_day(current):
            out.append(current)
        current += one
    if not out:
        raise LeanSidecarServiceError(f"date range {start}..{end} contains no NYSE trading days — nothing to stage")
    return out


def _hash_paths_in_workspace(workspace: Workspace, paths: list[Path]) -> tuple:
    """Hash a list of paths relative to the workspace data dir."""
    return hash_staged_files(workspace.data_dir, paths)


async def run_trusted_sample(
    request: TrustedRunRequest,
    *,
    on_phase: Callable[[str], None] | None = None,
    on_log: Callable[[str], None] | None = None,
) -> TrustedRunResult:
    """End-to-end trusted-sample run: stage → launch → write manifest.

    Pre-conditions:
      * ``PINNED_LEAN_IMAGE_DIGEST`` must be set in
        :mod:`app.lean_sidecar.config` (Phase 1b pinned the current
        digest; future image bumps go through the same flow).
      * Launcher process is reachable at ``LEAN_LAUNCHER_URL`` (the
        data plane reads this env var; the launcher binds to the same
        address).

    Post-conditions on success:
      * ``workspace/manifest.json`` exists with the full hash set;
      * the launcher's response is materialized as ``TrustedRunResult``;
      * the LEAN container is gone (``--rm``) and any leftover state
        lives entirely under the workspace.

    ``on_phase`` and ``on_log`` are optional progress hooks; the
    ``lean_engine_run`` job worker (:func:`app.routers.jobs.start_lean_engine_run_job`)
    passes the SSE stream emitters here so the Engine Lab run dock can
    surface coarse phase transitions in flight. Both callbacks must be
    cheap and non-blocking. When omitted the function behaves exactly
    as before — the existing ``/api/lean-sidecar/trusted-run`` endpoint
    (used by test infra and reconcile scripts) calls it with no hooks.
    """
    _emit_phase = on_phase or (lambda _name: None)
    _emit_log = on_log or (lambda _msg: None)

    if PINNED_LEAN_IMAGE_DIGEST is None:
        raise LeanSidecarServiceError(
            "PINNED_LEAN_IMAGE_DIGEST is not set; run scripts/lean_sidecar_pin_image.py first"
        )

    workspace = resolve_workspace(request.run_id, DEFAULT_ARTIFACTS_ROOT)
    # Reviewer P1: reject a reused run_id BEFORE staging touches the
    # workspace. Without this guard, ``ensure_layout`` silently no-ops
    # on an existing tree, the metadata + bars get re-staged on top,
    # and the parser then reads whatever ``*-summary.json`` the
    # previous run left behind — producing a fresh manifest that
    # claims new-bar consumption while reading stale LEAN output. We
    # check the run's root (not just workspace_dir) so legacy or
    # half-written runs that only created ``<root>/normalized/`` or
    # ``<root>/manifest.json`` still flag as taken.
    if workspace.root.exists():
        raise RunIdAlreadyUsedError(
            f"run_id {request.run_id!r} already has a workspace at "
            f"{workspace.root}; choose a fresh run_id (the UI's "
            "default ``runId`` field regenerates on every submit)."
        )
    workspace.ensure_layout()

    _emit_phase("staging_data")
    _emit_log(f"Staging LEAN fixtures for {request.symbol} {request.start_date}..{request.end_date}")

    # PR B: data-provenance knobs (source/session/adjusted) live on
    # ``request.data_policy``. ``data_policy.adjusted`` records the
    # staging-pipeline INTENT (spec § 4.4) — the LEAN runtime always
    # consumes Raw bars regardless of that flag. The pre-adjusted
    # staging pipeline that would honor ``adjusted=True`` lands in
    # Phase 2+; until then, ``fetch_canonical_minute_bars`` only
    # accepts ``adjustment="raw"`` (and the bundled trusted templates
    # also raise on anything else), so the runtime fetch is pinned
    # to ``"raw"`` via :func:`_runtime_polygon_adjustment`. The
    # vocabulary-consistency check below still allows
    # ``(adjusted=True, "Raw")`` per the PR B matrix.
    data_source = request.data_policy.source
    polygon_adjustment = _runtime_polygon_adjustment(request.data_policy)

    if data_source == "synthetic":
        trading_dates = _iter_trading_dates(request.start_date, request.end_date)
        bars_by_date = [(d, _generate_synthetic_bars(request.symbol, d)) for d in trading_dates]
    elif data_source == "polygon":
        from app.lean_sidecar.polygon_canonical import (
            fetch_canonical_minute_bars,
            get_default_provider,
        )

        provider = get_default_provider()
        bars_by_date = fetch_canonical_minute_bars(
            symbol=request.symbol,
            start_date=request.start_date,
            end_date=request.end_date,
            session=request.data_policy.session,
            adjustment=polygon_adjustment,
            provider=provider,
        )
        if not bars_by_date:
            raise LeanSidecarServiceError(
                f"polygon_returned_zero_bars: window={request.start_date.isoformat()}.."
                f"{request.end_date.isoformat()}; symbol={request.symbol}"
            )
        trading_dates = [d for d, _ in bars_by_date]
    else:
        # Defense-in-depth — Pydantic Literal already rejects unknown values.
        raise LeanSidecarServiceError(f"unknown data_source: {data_source!r}")

    bar_zip_paths = list(stage_minute_bars(workspace, symbol=request.symbol, bars_by_date=bars_by_date))
    # Phase 5c: stage synthetic minute QUOTE zips alongside the trade
    # zips. LEAN's default minute subscription requests both; without
    # the quote zip the log carries known-noise ``Cannot find file:
    # ...quote.zip`` warnings classified as ``failed_data_requests``.
    quote_zip_paths = list(stage_quote_bars(workspace, symbol=request.symbol, bars_by_date=bars_by_date))
    # Real OHLCV daily bars (open-of-first, high-max, low-min,
    # close-of-last, sum-of-volume), not the last minute bar copied.
    daily_bars = [_aggregate_daily_bar(request.symbol, day) for (_, day) in bars_by_date]
    daily_path = stage_daily_bars(workspace, symbol=request.symbol, bars=daily_bars)
    stage_lean_metadata_from_image(workspace, PINNED_LEAN_IMAGE_DIGEST)
    stage_empty_corporate_action_dirs(workspace)
    # Phase 4c: ``algorithm_source`` overrides the bundled trusted
    # sample when present. The Phase 1c sandbox shape (--read-only,
    # --user=<non-root>, --cap-drop=ALL, --network=none, workspace-
    # only mount) is what makes accepting arbitrary source safe.
    source_to_stage = request.algorithm_source if request.algorithm_source else _SOURCE_FOR_TEMPLATE[request.template]
    source_path = stage_algorithm_source(workspace, source_to_stage)
    # PR B: ``bar_minutes`` for the LEAN config is derived from
    # ``data_policy.strategy_bars.multiplier``. The bundled EMA template
    # pins multiplier=15; user-pasted source picks its own consolidator
    # and ignores this parameter.
    bar_minutes_for_config = request.data_policy.strategy_bars.multiplier
    config = LeanConfig(
        parameters={
            "start_date": request.start_date.isoformat(),
            "end_date": request.end_date.isoformat(),
            "starting_cash": str(request.starting_cash),
            "symbol": request.symbol,
            "bar_minutes": str(bar_minutes_for_config),
            "session": request.data_policy.session,
            "adjustment": polygon_adjustment,
        }
    )
    config_path = stage_lean_config(workspace, config)

    started_ms = now_ms_utc()
    launch_request = LaunchRequest(
        run_id=request.run_id,
        image_digest=PINNED_LEAN_IMAGE_DIGEST,
        cpus=DEFAULT_RUN_LIMITS.cpus,
        memory_mb=DEFAULT_RUN_LIMITS.memory_mb,
        pids_limit=DEFAULT_RUN_LIMITS.pids_limit,
        wall_clock_timeout_s=DEFAULT_RUN_LIMITS.wall_clock_timeout_s,
        workspace_max_mb=DEFAULT_RUN_LIMITS.workspace_max_mb,
        log_tail_bytes=DEFAULT_RUN_LIMITS.log_tail_bytes,
        # Intentionally NOT setting hardening_profile until we have a
        # verified fix for the wide-window SIGILL. The plumbing is in
        # place (HardeningProfile.WITH_TMPFS_256M_AND_APPLEHV_DOTNET_FIX
        # composes the tmpfs + the two ``DOTNET_*=0`` env flags) but
        # the 2026-06-09 in-session empirical bisection showed:
        # - Composer SIGILL on wide windows (2-month+) persists even
        #   with both DOTNET_* env flags set (verified at runner argv +
        #   container env levels). Backend's csc fix did not transfer
        #   to LEAN's runtime assembly load.
        # - The DOTNET_* flags introduce a GIL race in
        #   ``GC.RunFinalizers`` at shutdown on the previously-clean
        #   6-day window. Wiring them in by default would regress a
        #   working baseline.
        # The follow-up that finds the right env values (or a
        # different fix path — coredump, image rebuild, version bump)
        # should re-add ``hardening_profile=...`` here.
    )
    # Reviewer P1.3: write a manifest on EVERY exit path — success,
    # launcher-rejected, launcher-unreachable, even unexpected errors
    # below the launcher_client. Without this guard, a launcher that
    # rejected post-execute (e.g., workspace_max_mb_exceeded — the
    # container actually ran) leaves a fully-staged workspace on disk
    # with no manifest, no entry in the run-history sidebar, and no
    # rejection_reason audit. The failure manifest carries every
    # staged hash + a ``failure_reason`` note so the run remains
    # auditable and the operator can decide whether to retry, prune,
    # or escalate.
    from app.lean_sidecar.launcher_client import LauncherClientError

    response: LaunchResponse | None = None
    failure_reason: str | None = None
    launcher_exc: LauncherClientError | None = None
    _emit_phase("launching_sidecar")
    _emit_log(f"Submitting launch request to LEAN sidecar (image {PINNED_LEAN_IMAGE_DIGEST[:19]}…)")
    # ``sidecar_running`` is a back-to-back marker — ``post_launch`` is a
    # single blocking HTTP call that returns only after the launcher has
    # spawned, waited on, and reaped the container. From our side we
    # can't observe the launcher-accepted/container-started boundary,
    # but emitting the phase here lets the dock surface "the sidecar is
    # running now" elapsed-time UX rather than holding on
    # ``launching_sidecar`` for the entire run.
    _emit_phase("sidecar_running")
    try:
        response = await post_launch(launch_request)
    except LauncherClientError as e:
        launcher_exc = e
        failure_reason = f"{type(e).__name__}: {e}"
        logger.warning(
            "launcher call failed for %s: %s",
            request.run_id,
            failure_reason,
        )
    finished_ms = now_ms_utc()

    # Phase 3a: parse LEAN's output into typed DTOs and persist
    # them. Only attempt parsing when the container actually produced
    # output (exit_code 0); a crashed run leaves nothing useful to
    # parse and the inspection endpoint already 404s on absence.
    # NormalizedParserError is surfaced as a service-level error so
    # the operator sees "parser disagrees with LEAN schema" rather
    # than a silent missing-result.
    normalized: NormalizedResult | None = None
    normalized_path: Path | None = None
    if response is not None and response.exit_code == 0:
        _emit_phase("parsing_results")
        _emit_log("Parsing LEAN output")
        try:
            normalized = parse_workspace(workspace)
            normalized_path = write_normalized_result(workspace, normalized)
        except NormalizedParserError as e:
            logger.warning("normalized parser failed for %s: %s", request.run_id, e)
            # Don't fail the whole run — the raw LEAN artifacts are still
            # available via /runs/{id}/log. Phase 4 UI can fall back to
            # the unparsed artifacts when normalized is None.

    manifest = _build_manifest(
        request=request,
        workspace=workspace,
        bar_zip_paths=bar_zip_paths,
        quote_zip_paths=quote_zip_paths,
        daily_path=daily_path,
        source_path=source_path,
        config_path=config_path,
        response=response,
        started_ms=started_ms,
        finished_ms=finished_ms,
        normalized=normalized,
        # Phase 5d: the trading-date sequence that actually got staged.
        # Closes half of invariant #16 (staged-data window) — the
        # manifest can now show that the requested window vs the
        # staged window match (or surface that they don't).
        staged_trading_dates=trading_dates,
        failure_reason=failure_reason,
    )
    write_manifest(manifest, workspace.manifest_path)

    if launcher_exc is not None:
        # The failure manifest is on disk; surface the original
        # exception to the router so the HTTP layer maps to the right
        # status code (400 / 502 / 503) per the existing contract.
        raise launcher_exc

    # ``response`` is guaranteed non-None here because the only way to
    # leave it None is via ``launcher_exc``, which we re-raised above.
    assert response is not None

    # Task 1.10 — POST the run to .NET for persistence. This must happen
    # AFTER the manifest is finalized so workspace_path is stable. A
    # persistence failure is logged but does NOT abort the run — the
    # workspace artifacts on disk are the authoritative record.
    persist_payload = build_persist_payload(
        workspace_path=workspace.root,
        run_id=request.run_id,
        starting_cash=request.starting_cash,
        symbol=request.symbol,
        algorithm_name=_algorithm_name_for_run(request.template, request.algorithm_source),
        start_date_ms=_date_to_ms_utc(request.start_date),
        end_date_ms=_date_to_ms_utc(request.end_date),
        # PR B P1 fix — forward the manifest so the persist payload carries
        # the true ``brokerage_policy`` + ``data_policy``. Without this, the
        # .NET row would either be NULL ("unknown") or, pre-fix, would have
        # been silently labeled ``algorithm_default`` even for IBKR
        # reconciliation runs.
        manifest=manifest,
    )
    _emit_phase("persisting")
    _emit_log("Persisting run to history")
    strategy_execution_id = await persist_via_dotnet(
        payload=persist_payload,
        base_url=settings.BACKEND_URL,
    )

    return TrustedRunResult(
        run_id=request.run_id,
        is_clean=response.is_clean,
        exit_code=response.exit_code,
        duration_ms=response.duration_ms,
        timed_out=response.timed_out,
        lean_errors=dict(response.lean_errors),
        log_tail=response.log_tail,
        manifest_path=workspace.manifest_path,
        workspace_root=workspace.root,
        observations_path=workspace.object_store_dir / "observations.csv",
        lean_log_path=workspace.lean_log_path,
        normalized_path=normalized_path,
        normalized=normalized,
        strategy_execution_id=strategy_execution_id,
    )


def _build_data_policy(request: TrustedRunRequest) -> DataPolicy:
    """Return the request's ``data_policy`` after asserting vocabulary consistency.

    PR B (2026-05-19): the request already carries a fully-formed
    ``DataPolicy`` from the router. The orchestrator only re-asserts the
    adjustment-vocabulary matrix here so an upstream wiring bug surfaces
    at manifest construction time rather than silently producing a
    contradictory manifest.

    The LEAN template pins ``data_normalization_mode="Raw"`` regardless
    of the staging pipeline's ``adjusted`` flag, per PR B's pre-adjusted
    staging contract (see ``_assert_adjustment_vocabulary_consistent``).
    """
    _assert_adjustment_vocabulary_consistent(
        adjusted=request.data_policy.adjusted,
        data_normalization_mode="Raw",  # template pins Raw; widening is future work
    )
    return request.data_policy


def _build_manifest(
    *,
    request: TrustedRunRequest,
    workspace: Workspace,
    bar_zip_paths: list[Path],
    quote_zip_paths: list[Path],
    daily_path: Path,
    source_path: Path,
    config_path: Path,
    response: LaunchResponse | None,
    started_ms: int,
    finished_ms: int,
    normalized: NormalizedResult | None,
    staged_trading_dates: list[date],
    failure_reason: str | None = None,
) -> RunManifest:
    """Construct the full reproducibility manifest from the run.

    Field-order mirrors the ADR §"Reproducibility manifest" bullet
    list so a reviewer can grep against the authority doc.

    Reviewer P1.3: ``response`` is optional. When the launcher
    rejects or is unreachable before producing a ``LaunchResponse``,
    the orchestrator still calls this builder so a *failure manifest*
    lands on disk. The failure manifest records every byte that was
    actually staged + a ``failure_reason`` note so the run remains
    auditable and shows up in the sidebar instead of vanishing.
    """
    market_hours, symbol_properties = _list_metadata(workspace)
    if response is not None:
        exit_code = response.exit_code
        is_clean_note = f"is_clean={response.is_clean}"
        error_cats_note = f"lean_error_categories={sorted(response.lean_errors.keys())}"
    else:
        exit_code = None
        is_clean_note = "is_clean=False"
        error_cats_note = "lean_error_categories=[]"
    failure_note = (f"failure_reason={failure_reason}",) if failure_reason else ()
    bar_zips = _hash_paths_in_workspace(workspace, [*bar_zip_paths, *quote_zip_paths, daily_path])
    # PR A hardening: flatten the staged-zip hashes into a path-keyed
    # dict. The ``StagedDataManifest.bar_zips`` tuple is the
    # authoritative form; this index is a convenience for consumers
    # that want "what's the sha for this path?" without traversing
    # the tuple.
    staged_zip_sha256 = {sf.path_in_workspace: sf.sha256 for sf in bar_zips}
    return RunManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        run_id=request.run_id,
        algorithm_source_sha256=sha256_file(source_path),
        algorithm_type_name="MyAlgorithm",
        algorithm_language="Python",
        config_json_sha256=sha256_file(config_path),
        lean_image_digest=PINNED_LEAN_IMAGE_DIGEST or "",
        launcher_version_sha256=LAUNCHER_VERSION_HASH,
        normalized_parser_version=NORMALIZED_PARSER_VERSION,
        staged_data=StagedDataManifest(
            # Phase 5c: include the quote zips alongside trade + daily
            # in the manifest's staged-data hash list. Reproducibility
            # requires every byte LEAN saw to be hashed.
            bar_zips=bar_zips,
            market_hours_database=(
                _hash_paths_in_workspace(workspace, [market_hours])[0] if market_hours is not None else None
            ),
            symbol_properties_database=(
                _hash_paths_in_workspace(workspace, [symbol_properties])[0] if symbol_properties is not None else None
            ),
        ),
        data_policy=_build_data_policy(request),
        # Trusted sample stages raw deci-cent bars without factor/map
        # adjustments; this is the non-reconciliation policy.
        data_adjustment_policy="pre_adjusted_non_reconciliation",
        data_normalization_mode="Raw",
        fill_forward=False,
        # Phase 5b: when the caller pastes their own source, we can't
        # introspect its SetBrokerageModel call, so the manifest
        # records ``algorithm_default`` (the brokerage choice is in
        # the source's hash, captured above). When using a bundled
        # template, the template selector pins the policy exactly.
        brokerage_policy=(
            "algorithm_default" if request.algorithm_source else _BROKERAGE_POLICY_FOR_TEMPLATE[request.template]
        ),
        starting_capital=request.starting_cash,
        account_currency="USD",
        limits={
            "cpus": DEFAULT_RUN_LIMITS.cpus,
            "memory_mb": DEFAULT_RUN_LIMITS.memory_mb,
            "pids_limit": DEFAULT_RUN_LIMITS.pids_limit,
            "wall_clock_timeout_s": DEFAULT_RUN_LIMITS.wall_clock_timeout_s,
            "workspace_max_mb": DEFAULT_RUN_LIMITS.workspace_max_mb,
            "log_tail_bytes": DEFAULT_RUN_LIMITS.log_tail_bytes,
        },
        parameters={
            "start_date": request.start_date.isoformat(),
            "end_date": request.end_date.isoformat(),
            "starting_cash": request.starting_cash,
            "symbol": request.symbol,
            # PR B: ``bar_minutes`` and ``adjustment`` are derived from the
            # canonical ``data_policy`` block; the manifest's parameter
            # echo keeps the LEAN-config legacy keys so downstream
            # reconciliation readers don't need to branch on schema
            # version yet.
            "bar_minutes": request.data_policy.strategy_bars.multiplier,
            "session": request.data_policy.session,
            "adjustment": "raw" if not request.data_policy.adjusted else "adjusted",
        },
        # API boundary timestamps are already int64 ms UTC; pass them
        # through unchanged so the manifest's recorded window matches
        # the request exactly. The router enforces start < end strictly,
        # so WindowMs's invariant always holds.
        requested_window_ms=WindowMs(
            start_ms=request.start_ms_utc,
            end_ms=request.end_ms_utc,
        ),
        # ``effective_algorithm_window_ms`` derived from the parsed
        # equity curve when available. Phase 5d/5e together close
        # invariant #16: ``staged_data_window_ms`` is the ET-midnight
        # envelope of staged trading days (5d), and
        # ``bars_consumed_by_symbol`` is the observations.csv line
        # count (5e). Reconciliation readers can now diff
        # requested vs staged vs effective windows AND see whether
        # bars were actually consumed.
        effective_algorithm_window_ms=_effective_window_from_normalized(normalized),
        staged_data_window_ms=_staged_window_from_dates(staged_trading_dates),
        bars_consumed_by_symbol=_count_bars_consumed(workspace, request.symbol),
        staged_zip_sha256=staged_zip_sha256,
        started_at_ms=started_ms,
        finished_at_ms=finished_ms,
        exit_code=exit_code,
        notes=(
            "Phase 3a — normalized parser populates effective_algorithm_window_ms; "
            "staged_data_window_ms and bars_consumed_by_symbol still pending Phase 3b.",
            is_clean_note,
            error_cats_note,
            f"normalized_parser={'present' if normalized else 'absent'}",
            # Phase 4c audit: distinguishes user-provided source from
            # the trusted sample. The source hash above
            # (algorithm_source_sha256) already records the *content*,
            # but this note makes the intent explicit for audit.
            f"algorithm_source_kind={'user_provided' if request.algorithm_source else 'trusted_sample'}",
            # Phase 5b: which bundled template (if any) was staged.
            # ``user_provided_no_template`` when caller pasted source.
            f"trusted_template={'user_provided_no_template' if request.algorithm_source else request.template}",
            # P2.5: tag the date-window contract this manifest was
            # written under so the cross-engine reconciler can branch
            # on contract without inspecting ms values.
            P2_5_DATE_SEMANTICS_NOTE,
            *failure_note,
        ),
    )


def _effective_window_from_normalized(normalized: NormalizedResult | None) -> WindowMs | None:
    """Derive the effective-algorithm window from the parsed equity curve.

    LEAN samples equity at bar boundaries; the first and last points
    are the algorithm's actual run window after any internal date
    clipping. Returning ``None`` when the curve is empty or the
    parser didn't run keeps the manifest faithful — a missing window
    is not the same as a window of length zero.
    """
    if normalized is None or normalized.first_equity_ms_utc is None:
        return None
    if normalized.last_equity_ms_utc is None:
        return None
    # WindowMs requires start < end; if LEAN somehow emitted a
    # single-point curve, return None rather than fabricate a window.
    if normalized.first_equity_ms_utc >= normalized.last_equity_ms_utc:
        return None
    return WindowMs(
        start_ms=normalized.first_equity_ms_utc,
        end_ms=normalized.last_equity_ms_utc,
    )


def _list_metadata(workspace: Workspace) -> tuple[Path | None, Path | None]:
    """Return (market_hours, symbol_properties) paths from the workspace."""
    from app.lean_sidecar.staging import list_metadata_databases

    return list_metadata_databases(workspace)
