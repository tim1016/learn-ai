"""Regenerate cross-engine golden-fixture cells.

Per-cell sequence (Task 7 ships the orchestration shape; Task 10 fills
the LEAN/Engine staging stubs):

  1. Pre-flight: verify _lean_data_capture/<TICKER>/ exists and its
     data_contract_hash matches the capture manifest.
  2. Stage LEAN sidecar run for (ticker, window) into a temp dir.
  3. Run Engine Lab live against the same capture, into another temp dir.
  4. Run all three gates via run_cell_gates.
  5. On pass: write reconciliation_pinned.json, replace the committed
     cell directory atomically, update manifest.json with new artifact
     hashes.
  6. On fail: emit failure report to a sibling .failed/ dir; exit
     non-zero; leave committed cell directory untouched.

Reference: docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md
            § "Regeneration workflow"
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Self-bootstrap: when invoked as `python scripts/regenerate_cross_engine_study.py`,
# the script's parent (PythonDataService/) needs to be on sys.path so the
# `app.*` imports resolve. Insert at index 0 (highest priority) but ONLY
# if not already present, so an external PYTHONPATH override still wins.
_REPO_PYTHON = Path(__file__).resolve().parent.parent
if str(_REPO_PYTHON) not in sys.path:
    sys.path.insert(0, str(_REPO_PYTHON))

import argparse  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from collections import Counter  # noqa: E402
from datetime import UTC, date, datetime  # noqa: E402
from decimal import Decimal  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from app.lean_sidecar.config import (  # noqa: E402
    DEFAULT_ARTIFACTS_ROOT,
    DEFAULT_RUN_LIMITS,
    PINNED_LEAN_IMAGE_DIGEST,
)
from app.lean_sidecar.cross_runner import CrossRunOrderEvent, run_engine_lab_on_workspace  # noqa: E402
from app.lean_sidecar.launcher.models import LaunchRequest  # noqa: E402
from app.lean_sidecar.launcher.service import launch  # noqa: E402
from app.lean_sidecar.lean_config import LeanConfig  # noqa: E402
from app.lean_sidecar.normalized_parser import parse_workspace  # noqa: E402
from app.lean_sidecar.parity_matrix.cell_runner import (  # noqa: E402
    CellRunReport,
    run_cell_gates,
)
from app.lean_sidecar.parity_matrix.manifest import (  # noqa: E402
    CellManifest,
    sha256_of_file,
    sha256_of_text,
)
from app.lean_sidecar.parity_matrix.matrix import CELLS, Cell, cell_by_id  # noqa: E402
from app.lean_sidecar.staging import (  # noqa: E402
    stage_algorithm_source,
    stage_lean_config,
    stage_lean_metadata_from_image,
)
from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE  # noqa: E402
from app.lean_sidecar.workspace import resolve_workspace  # noqa: E402

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "PythonDataService" / "tests" / "fixtures" / "golden" / "cross-engine-studies"
_ET = ZoneInfo("America/New_York")

# A full US-equity regular session is 390 one-minute bars; NYSE half-days
# (~210 bars) are the realistic floor. A session well under this signals a
# partial/truncated stream rather than a holiday short session.
_MIN_SESSION_BARS = 200


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Regenerate cross-engine golden-fixture cells.")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Regenerate all 12 cells.")
    group.add_argument("--cell", type=str, help="Regenerate one cell by cell_id.")
    group.add_argument(
        "--ticker",
        type=str,
        help="Regenerate all three cells for one ticker.",
    )
    return p.parse_args(argv)


def _resolve_target_cells(ns: argparse.Namespace) -> list[Cell]:
    """Resolve the CLI namespace to a list of Cells to regenerate.

    Raises KeyError when ``--cell`` names an unknown cell. Returns empty
    list for an unknown ``--ticker`` — main() decides whether to error.
    """
    if ns.all:
        return list(CELLS)
    if ns.cell:
        return [cell_by_id(ns.cell)]
    if ns.ticker:
        return [c for c in CELLS if c.ticker == ns.ticker]
    raise SystemExit("no target specified")


def _stage_lean_run(cell: Cell, output_dir: Path) -> None:
    """Run LEAN sidecar for one cell; write outputs to output_dir/lean/.

    Sequence:
      1. Resolve a fresh run_id and workspace under DEFAULT_ARTIFACTS_ROOT.
      2. Symlink (or copy) the capture's data dir into workspace/data/.
      3. Stage algorithm source, LEAN config, and image metadata.
      4. Launch the LEAN container via the launcher service (in-process).
      5. Read ObjectStore outputs (observations.csv, state.csv) and the
         parsed order events; write them to output_dir/lean/.

    The launcher is called in-process (``launcher.service.launch``) rather
    than over HTTP so the script has no network dependency beyond the LEAN
    container invocation itself. The workspace ends up under
    DEFAULT_ARTIFACTS_ROOT (same root the launcher normally uses).
    """
    if PINNED_LEAN_IMAGE_DIGEST is None:
        raise RuntimeError("PINNED_LEAN_IMAGE_DIGEST is not set; run scripts/lean_sidecar_pin_image.py first")

    capture = FIXTURE_ROOT / "_lean_data_capture" / cell.ticker

    # Unique run_id: must be lowercase alphanumeric + hyphens, 3–64 chars.
    run_id = f"regen-{cell.ticker.lower()}-{uuid.uuid4().hex[:12]}"
    workspace = resolve_workspace(run_id, DEFAULT_ARTIFACTS_ROOT)
    workspace.ensure_layout()

    # Copy the capture's data tree into workspace/data/.  Copying (not
    # symlinking) ensures the workspace is self-contained and the
    # container's read-only bind mount sees stable files even if the
    # capture is on a different filesystem.
    # ensure_layout() already created an empty data_dir; remove it so
    # copytree can populate the directory from scratch.
    logger.info("  copying capture data → workspace/data/ ...")
    shutil.rmtree(workspace.data_dir)
    shutil.copytree(str(capture), str(workspace.data_dir))

    # Stage image metadata (market-hours, symbol-properties) from the
    # pinned LEAN image so LEAN's initialization succeeds.
    logger.info("  staging LEAN image metadata ...")
    stage_lean_metadata_from_image(workspace, PINNED_LEAN_IMAGE_DIGEST)

    # Stage algorithm source.
    stage_algorithm_source(workspace, EMA_CROSSOVER_SOURCE)

    # Build and write LEAN config.
    config = LeanConfig(
        parameters={
            "start_date": cell.start_date.isoformat(),
            "end_date": cell.end_date.isoformat(),
            "starting_cash": "100000",
            "symbol": cell.ticker,
            "bar_minutes": "15",
            "session": "regular",
            "adjustment": "raw",
        }
    )
    stage_lean_config(workspace, config)

    # Launch LEAN via the in-process launcher service.  Using in-process
    # launch (not HTTP) keeps the script dependency-free w.r.t. a running
    # launcher service.
    logger.info("  launching LEAN container (run_id=%s) ...", run_id)
    launch_request = LaunchRequest(
        run_id=run_id,
        image_digest=PINNED_LEAN_IMAGE_DIGEST,
        cpus=DEFAULT_RUN_LIMITS.cpus,
        memory_mb=DEFAULT_RUN_LIMITS.memory_mb,
        pids_limit=DEFAULT_RUN_LIMITS.pids_limit,
        wall_clock_timeout_s=DEFAULT_RUN_LIMITS.wall_clock_timeout_s,
        workspace_max_mb=DEFAULT_RUN_LIMITS.workspace_max_mb,
        log_tail_bytes=DEFAULT_RUN_LIMITS.log_tail_bytes,
        hardening_profile="with_tmpfs_256m",
    )
    t0 = time.monotonic()
    launch_resp = launch(launch_request, artifacts_root=DEFAULT_ARTIFACTS_ROOT)
    elapsed_s = time.monotonic() - t0
    logger.info("  LEAN exit_code=%d  is_clean=%s  %.0fs", launch_resp.exit_code, launch_resp.is_clean, elapsed_s)

    if not launch_resp.is_clean:
        raise RuntimeError(
            f"LEAN run for {cell.cell_id} failed "
            f"(exit_code={launch_resp.exit_code}, timed_out={launch_resp.timed_out}). "
            f"Log tail:\n{launch_resp.log_tail[-2000:]}"
        )

    # Read the ObjectStore artifacts LEAN wrote.
    obj_store = workspace.object_store_dir
    obs_src = obj_store / "observations.csv"
    state_src = obj_store / "state.csv"
    if not obs_src.exists():
        raise RuntimeError(f"LEAN did not emit observations.csv under {obj_store}")
    if not state_src.exists():
        raise RuntimeError(f"LEAN did not emit state.csv under {obj_store}")

    # Parse LEAN's result.json for the normalized order events.
    normalized = parse_workspace(workspace)
    orders_payload = [e.model_dump(mode="json", by_alias=False) for e in normalized.order_events]

    # Write outputs to output_dir/lean/.
    lean_out = output_dir / "lean"
    lean_out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(obs_src), str(lean_out / "observations.csv"))
    shutil.copy2(str(state_src), str(lean_out / "state.csv"))
    (lean_out / "orders.json").write_text(
        json.dumps(orders_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    logger.info("  LEAN outputs written to %s", lean_out)


def _run_engine_live(cell: Cell, output_dir: Path) -> list[CrossRunOrderEvent]:
    """Run Engine Lab via cross_runner; write outputs to output_dir/engine/.

    Returns the normalized order events for Gate 3.

    The strategy emits observations.csv + state.csv to eng_dir when
    output_dir is threaded through to the constructor (Task 10 extension
    of run_engine_lab_on_workspace).
    """
    eng_dir = output_dir / "engine"
    eng_dir.mkdir(parents=True, exist_ok=True)
    capture = FIXTURE_ROOT / "_lean_data_capture" / cell.ticker

    t0 = time.monotonic()
    result = run_engine_lab_on_workspace(
        workspace_path=capture,
        strategy_class_name="SpyEmaCrossoverAlgorithm",
        symbol=cell.ticker,
        start_date=cell.start_date,
        end_date=cell.end_date,
        initial_cash=Decimal(100000),
        output_dir=eng_dir,
    )
    elapsed_s = time.monotonic() - t0
    logger.info("  Engine Lab: %d fills  %.1fs", result.total_order_events, elapsed_s)
    return list(result.order_events)


def _capture_session_dates(cell: Cell) -> set[date]:
    """Trading-session dates inside the cell window, from the capture's minute zips."""
    minute_dir = FIXTURE_ROOT / "_lean_data_capture" / cell.ticker / "equity" / "usa" / "minute" / cell.ticker.lower()
    sessions: set[date] = set()
    for zip_path in minute_dir.glob("*_trade.zip"):
        stamp = zip_path.name[:8]
        d = date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))
        if cell.start_date <= d <= cell.end_date:
            sessions.add(d)
    return sessions


def _assert_lean_observations_complete(cell: Cell, lean_dir: Path) -> None:
    """Fail loudly if the LEAN observations stream is truncated.

    A clean LEAN exit code does not prove a complete backtest: a corrupt
    factor file (zero reference price) kills the data subscription
    mid-run while LEAN still exits 0 and prints statistics. This check
    compares the observation date set against the capture's trading
    sessions in the cell window — every session must be present, and
    each must carry a full regular-session bar count.
    """
    obs_path = lean_dir / "observations.csv"
    counts: Counter[date] = Counter()
    with obs_path.open("r", encoding="utf-8") as f:
        next(f, None)  # header
        for line in f:
            line = line.strip()
            if not line:
                continue
            ms = int(line.split(",", 1)[0])
            et_date = datetime.fromtimestamp(ms / 1000, tz=UTC).astimezone(_ET).date()
            counts[et_date] += 1

    expected = _capture_session_dates(cell)
    observed = set(counts)
    missing = sorted(expected - observed)
    extra = sorted(observed - expected)
    if missing or extra:
        raise RuntimeError(
            f"{cell.cell_id}: LEAN observations do not cover the cell window — "
            f"{len(missing)} missing session(s)"
            + (f" (first {missing[0]}, last {missing[-1]})" if missing else "")
            + f", {len(extra)} unexpected session(s)"
            + (f" (first {extra[0]}, last {extra[-1]})" if extra else "")
            + ". A truncated stream usually means LEAN's data feed died "
            "mid-run — e.g. a zero-reference-price dividend in the factor file."
        )

    thin = sorted(d for d, n in counts.items() if n < _MIN_SESSION_BARS)
    if thin:
        raise RuntimeError(
            f"{cell.cell_id}: {len(thin)} trading session(s) carry fewer than "
            f"{_MIN_SESSION_BARS} observation bars (first {thin[0]}, "
            f"{counts[thin[0]]} bars); regular-session coverage is incomplete."
        )


def _build_manifest_dict(cell: Cell, staging: Path) -> dict:
    """Build the CellManifest payload with fresh artifact hashes.

    All four artifact files (orders.json, state.csv, observations.csv,
    reconciliation_pinned.json) must already exist under staging/lean/
    and staging/ respectively before this is called — _write_cell_atomically
    ensures that order.

    The dict is validated against CellManifest before returning; a
    Pydantic ValidationError propagates directly so the caller sees the
    exact field that failed rather than a generic "manifest invalid" message.
    """

    # --- git provenance ---
    def _git(args: list[str]) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git_commit = _git(["rev-parse", "HEAD"])
    captured_by = _git(["config", "user.name"])

    # --- capture data_contract_hash from its manifest.json ---
    capture_manifest_path = FIXTURE_ROOT / "_lean_data_capture" / cell.ticker / "manifest.json"
    capture_manifest = json.loads(capture_manifest_path.read_text(encoding="utf-8"))
    data_contract_hash: str = capture_manifest["data_contract_hash"]

    # --- artifact hashes (all files must exist at this point) ---
    lean_dir = staging / "lean"
    artifacts_dict = {
        "orders_sha256": sha256_of_file(lean_dir / "orders.json"),
        "state_sha256": sha256_of_file(lean_dir / "state.csv"),
        "observations_sha256": sha256_of_file(lean_dir / "observations.csv"),
        "reconciliation_sha256": sha256_of_file(staging / "reconciliation_pinned.json"),
    }

    # --- trading_days_expected: exact count from the captured session files ---
    # The capture's minute zips are the authoritative session list for this
    # window; an exact count keeps the manifest accurate (a 252/365 estimate
    # drifts from the real fixture).
    trading_days_expected = len(_capture_session_dates(cell))

    # --- LEAN image digest in the required format ---
    bare_digest = PINNED_LEAN_IMAGE_DIGEST  # "sha256:..."
    if not bare_digest.startswith("sha256:"):
        raise RuntimeError(f"PINNED_LEAN_IMAGE_DIGEST unexpected format: {bare_digest!r}")
    container_image_digest = f"docker.io/quantconnect/lean@{bare_digest}"

    # --- strategy constants and runtime parameters (mirror EMA_CROSSOVER_SOURCE) ---
    parameters_constants: dict[str, int | float] = {
        "FAST_PERIOD": 5,
        "SLOW_PERIOD": 10,
        "RSI_PERIOD": 14,
        "EXIT_BARS": 5,
        "GAP_MIN": 0.20,
        "RSI_LO": 50,
        "RSI_HI": 70,
    }
    runtime_parameters: dict[str, str | int | float] = {
        "symbol": cell.ticker,
        "bar_minutes": 15,
        "session": "regular",
        "adjustment": "raw",
        "starting_cash": 100000,
        # Pinned: the Engine sizes SetHoldings like LEAN (free-portfolio
        # buffer + order fee). Gate 3 holds qty_atol=0 against this.
        "sizing_model": "lean_set_holdings",
    }

    # --- state_csv_schema (matches state.csv header written by both engines) ---
    state_csv_schema = {
        "columns": ["ts_ms_utc", "close", "ema_fast", "ema_slow", "rsi", "cross_state", "signal"],
        "column_types": {
            "ts_ms_utc": "int64",
            "close": "float64",
            "ema_fast": "float64",
            "ema_slow": "float64",
            "rsi": "float64",
            "cross_state": "str",
            "signal": "str",
        },
    }

    manifest_dict = {
        "schema_version": 1,
        "cell_id": cell.cell_id,
        "ticker": cell.ticker,
        "window": {
            "label": cell.window_label.value,
            "start_date": cell.start_date.isoformat(),
            "end_date": cell.end_date.isoformat(),
            "session": "regular",
            "trading_days_expected": trading_days_expected,
        },
        "strategy": {
            "trusted_sample": "EMA_CROSSOVER_SOURCE",
            "trusted_sample_source_sha256": sha256_of_text(EMA_CROSSOVER_SOURCE),
            "parameters_constants": parameters_constants,
            "runtime_parameters": runtime_parameters,
        },
        "data": {
            "lean_data_capture_ref": f"_lean_data_capture/{cell.ticker}",
            "data_contract_hash": data_contract_hash,
        },
        "broker": {
            "brokerage_model": "InteractiveBrokersBrokerage",
            "account_type": "Margin",
            "fill_model": "ImmediateFillModel",
            "fee_model": "InteractiveBrokersFeeModel",
        },
        "lean_runtime": {
            "container_image_digest": container_image_digest,
        },
        "artifacts": artifacts_dict,
        "state_csv_schema": state_csv_schema,
        "timezone": "America/New_York",
        "timestamp_convention": "int64_ms_utc",
        "fixture_git_commit": git_commit,
        "python_data_service_commit": git_commit,
        "generator_script_sha256": sha256_of_file(Path(__file__).resolve()),
        "captured_by": captured_by,
        "captured_at_ms_utc": int(datetime.now(UTC).timestamp() * 1000),
    }

    # Validate against the Pydantic schema — fail fast with a clear error.
    CellManifest(**manifest_dict)
    return manifest_dict


def _write_cell_atomically(
    *,
    cell: Cell,
    staged_lean_dir: Path,
    reconciliation: dict,
) -> None:
    """Replace the committed cell directory in one rename; write
    manifest.json + attribution.md + reconciliation_pinned.json.

    Requires `_build_manifest_dict` (Task 10 stub) to be implemented before
    this function can complete end-to-end.
    """
    target = FIXTURE_ROOT / "cells" / cell.cell_id
    staging = FIXTURE_ROOT / "cells" / f".{cell.cell_id}.new"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    shutil.copytree(staged_lean_dir, staging / "lean")
    (staging / "reconciliation_pinned.json").write_text(
        json.dumps(reconciliation, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    manifest_dict = _build_manifest_dict(cell, staging)
    (staging / "manifest.json").write_text(
        json.dumps(manifest_dict, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (staging / "attribution.md").write_text(
        f"# {cell.cell_id} attribution\n\n"
        f"Regenerated by `regenerate_cross_engine_study.py`. "
        f"See `manifest.json` for the full provenance block.\n",
        encoding="utf-8",
    )

    # Crash-safe swap: move the old cell aside to a backup, promote the
    # staging dir, then drop the backup. If the promote raises, the backup
    # is restored — an interrupted run never leaves the cell missing.
    backup = FIXTURE_ROOT / "cells" / f".{cell.cell_id}.bak"
    if backup.exists():
        shutil.rmtree(backup)
    if target.exists():
        target.rename(backup)
    try:
        staging.rename(target)
    except OSError:
        if backup.exists() and not target.exists():
            backup.rename(target)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def _emit_failure_report(cell: Cell, report: CellRunReport, root: Path) -> None:
    """Write a JSON failure report under root/.failed/<cell_id>/."""
    failure_dir = root / ".failed" / cell.cell_id
    failure_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "cell_id": cell.cell_id,
        "overall_passed": report.overall_passed,
        "observations_passed": report.observations.passed,
        "state_passed": (report.state.passed if report.state is not None else None),
        "trade_passed": (report.trade.passed if report.trade is not None else None),
        "observations_failures": [
            {"row_index": f.row_index, "field": f.field, "reason": f.reason} for f in report.observations.failures
        ],
        "state_failures": (
            [
                {
                    "row_index": f.row_index,
                    "field": f.field,
                    "reason": f.reason,
                }
                for f in report.state.failures
            ]
            if report.state is not None
            else None
        ),
    }
    (failure_dir / "report.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def regenerate_one_cell(cell: Cell) -> bool:
    """Regenerate one cell. Returns True on pass, False on fail."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        lean_out = tmp_root / "lean_staged"
        lean_out.mkdir()
        eng_out = tmp_root / "engine_staged"
        eng_out.mkdir()

        try:
            _stage_lean_run(cell, lean_out)
            # Post-run completeness gate: a clean exit code is not proof the
            # backtest covered the whole window (see _assert_lean_observations_complete).
            _assert_lean_observations_complete(cell, lean_out / "lean")
            engine_orders = _run_engine_live(cell, eng_out)
        except RuntimeError as exc:
            # A LEAN failure or a truncated observation stream. Record it and
            # return False so an --all run still reports every other cell
            # instead of aborting the whole batch on the first bad one.
            failure_dir = FIXTURE_ROOT / ".failed" / cell.cell_id
            failure_dir.mkdir(parents=True, exist_ok=True)
            (failure_dir / "error.txt").write_text(str(exc), encoding="utf-8")
            logger.error("  staging failed: %s", exc)
            return False

        report = run_cell_gates(
            pinned_lean_dir=lean_out / "lean",
            engine_output_dir=eng_out / "engine",
            engine_normalized_orders=engine_orders,
        )

        if not report.overall_passed:
            if report.trade is not None:
                logger.error(
                    "  Gate 3 trade: %d gating divergences | counts_by_category=%s",
                    report.trade.gating_divergent_count,
                    {c.value: n for c, n in report.trade.counts_by_category.items()},
                )
                for d in report.trade.divergences[:8]:
                    logger.error("    %s %s: %s", d.trading_date.isoformat(), d.category.value, d.detail)
            _emit_failure_report(cell, report, FIXTURE_ROOT)
            return False

        reconciliation = {
            "status": "passed",
            "trade_summary": (
                {
                    "passed": report.trade.passed,
                    "gating_divergent_count": getattr(report.trade, "gating_divergent_count", None),
                }
                if report.trade is not None
                else None
            ),
            "captured_at_ms_utc": int(datetime.now(UTC).timestamp() * 1000),
        }
        _write_cell_atomically(
            cell=cell,
            staged_lean_dir=lean_out / "lean",
            reconciliation=reconciliation,
        )
        return True


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",  # Plain output matches the previous print() format
    )
    ns = _parse_args(argv if argv is not None else sys.argv[1:])
    cells = _resolve_target_cells(ns)
    if not cells:
        logger.error("No cells matched the selection.")
        return 2
    logger.info("Regenerating %d cell(s): %s", len(cells), [c.cell_id for c in cells])
    failures: list[str] = []
    for c in cells:
        logger.info("--- %s ---", c.cell_id)
        if regenerate_one_cell(c):
            logger.info("  passed")
        else:
            failures.append(c.cell_id)
            logger.error("  FAILED — see .failed/%s/report.json", c.cell_id)
    if failures:
        logger.error("\n%d cell(s) failed: %s", len(failures), failures)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
