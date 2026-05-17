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
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

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
    RunManifest,
    StagedDataManifest,
    WindowMs,
    hash_staged_files,
    now_ms_utc,
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
)
from app.lean_sidecar.trusted_samples.buy_and_hold import BUY_AND_HOLD_SOURCE
from app.lean_sidecar.workspace import Workspace, resolve_workspace

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


@dataclass(frozen=True, slots=True)
class TrustedRunRequest:
    """Phase 2a input — bounded set of caller-tunable knobs.

    ``start_ms_utc`` and ``end_ms_utc`` are int64 ms UTC per the repo's
    timestamp rigor rule. They are converted to ``date`` *inside this
    module* (under the boundary) so the LEAN config's ISO-string
    parameter values stay consistent with what the trusted-sample
    algorithm reads via ``GetParameter``.
    """

    run_id: str
    symbol: str
    start_ms_utc: int
    end_ms_utc: int
    starting_cash: float

    @property
    def start_date(self) -> date:
        """Trading date corresponding to ``start_ms_utc`` (UTC calendar day)."""
        return datetime.fromtimestamp(self.start_ms_utc / 1000, tz=UTC).date()

    @property
    def end_date(self) -> date:
        """Trading date corresponding to ``end_ms_utc`` (UTC calendar day)."""
        return datetime.fromtimestamp(self.end_ms_utc / 1000, tz=UTC).date()


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


def _iter_trading_dates(start: date, end: date) -> list[date]:
    """Inclusive trading-date sequence, weekends-only filtered.

    Phase 2a does not consult an exchange-holiday calendar — the
    trusted sample window must be operator-chosen as actual trading
    days. If the start/end span includes a US holiday, the run still
    completes (LEAN ignores days without staged data) but the
    bar-consumption count drops, which the response surfaces.
    """
    out: list[date] = []
    current = start
    one = timedelta(days=1)
    while current <= end:
        # Weekday 0–4 are Mon–Fri; LEAN equity-data layout is M–F only.
        if current.weekday() < 5:
            out.append(current)
        current += one
    if not out:
        raise LeanSidecarServiceError(f"date range {start}..{end} contains no weekdays — nothing to stage")
    return out


def _hash_paths_in_workspace(workspace: Workspace, paths: list[Path]) -> tuple:
    """Hash a list of paths relative to the workspace data dir."""
    return hash_staged_files(workspace.data_dir, paths)


async def run_trusted_sample(request: TrustedRunRequest) -> TrustedRunResult:
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
    """
    if PINNED_LEAN_IMAGE_DIGEST is None:
        raise LeanSidecarServiceError(
            "PINNED_LEAN_IMAGE_DIGEST is not set; run scripts/lean_sidecar_pin_image.py first"
        )

    workspace = resolve_workspace(request.run_id, DEFAULT_ARTIFACTS_ROOT)
    workspace.ensure_layout()

    trading_dates = _iter_trading_dates(request.start_date, request.end_date)
    bars_by_date = [(d, _generate_synthetic_bars(request.symbol, d)) for d in trading_dates]
    bar_zip_paths = list(stage_minute_bars(workspace, symbol=request.symbol, bars_by_date=bars_by_date))
    # Real OHLCV daily bars (open-of-first, high-max, low-min,
    # close-of-last, sum-of-volume), not the last minute bar copied.
    daily_bars = [_aggregate_daily_bar(request.symbol, day) for (_, day) in bars_by_date]
    daily_path = stage_daily_bars(workspace, symbol=request.symbol, bars=daily_bars)
    stage_lean_metadata_from_image(workspace, PINNED_LEAN_IMAGE_DIGEST)
    stage_empty_corporate_action_dirs(workspace)
    source_path = stage_algorithm_source(workspace, BUY_AND_HOLD_SOURCE)
    config = LeanConfig(
        parameters={
            "start_date": request.start_date.isoformat(),
            "end_date": request.end_date.isoformat(),
            "starting_cash": str(request.starting_cash),
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
    )
    response: LaunchResponse = await post_launch(launch_request)
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
    if response.exit_code == 0:
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
        daily_path=daily_path,
        source_path=source_path,
        config_path=config_path,
        response=response,
        started_ms=started_ms,
        finished_ms=finished_ms,
        normalized=normalized,
    )
    write_manifest(manifest, workspace.manifest_path)

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
    )


def _build_manifest(
    *,
    request: TrustedRunRequest,
    workspace: Workspace,
    bar_zip_paths: list[Path],
    daily_path: Path,
    source_path: Path,
    config_path: Path,
    response: LaunchResponse,
    started_ms: int,
    finished_ms: int,
    normalized: NormalizedResult | None,
) -> RunManifest:
    """Construct the full reproducibility manifest from the run.

    Field-order mirrors the ADR §"Reproducibility manifest" bullet
    list so a reviewer can grep against the authority doc.
    """
    market_hours, symbol_properties = _list_metadata(workspace)
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
            bar_zips=_hash_paths_in_workspace(workspace, [*bar_zip_paths, daily_path]),
            market_hours_database=(
                _hash_paths_in_workspace(workspace, [market_hours])[0] if market_hours is not None else None
            ),
            symbol_properties_database=(
                _hash_paths_in_workspace(workspace, [symbol_properties])[0] if symbol_properties is not None else None
            ),
        ),
        # Trusted sample stages raw deci-cent bars without factor/map
        # adjustments; this is the non-reconciliation policy.
        data_adjustment_policy="pre_adjusted_non_reconciliation",
        data_normalization_mode="Raw",
        fill_forward=False,
        brokerage_policy="algorithm_default",
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
        # equity curve when available; ``bars_consumed_by_symbol``
        # is still empty until Phase 3b adds per-symbol bar counts
        # from the observations.csv audit file.
        effective_algorithm_window_ms=_effective_window_from_normalized(normalized),
        bars_consumed_by_symbol={},
        started_at_ms=started_ms,
        finished_at_ms=finished_ms,
        exit_code=response.exit_code,
        notes=(
            "Phase 3a — normalized parser populates effective_algorithm_window_ms; "
            "staged_data_window_ms and bars_consumed_by_symbol still pending Phase 3b.",
            f"is_clean={response.is_clean}",
            f"lean_error_categories={sorted(response.lean_errors.keys())}",
            f"normalized_parser={'present' if normalized else 'absent'}",
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
