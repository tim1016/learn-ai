"""Command-line entrypoint for the live runtime.

Implements the operator-facing CLI for the IBKR paper-shadow run.

Subcommands:
  * ``init-ledger`` — build and write ``run_ledger.json`` for a new run.
    Refuses if the source tree is dirty (§ 9 dirty-tree halt) so the
    captured ``code_sha`` is meaningful.
  * ``pre-flight`` — run the morning-gate halt checks (§ 6.4 + § 9)
    against an existing run directory. Exits non-zero on halt.
  * ``start`` — connect to IBKR Gateway and run the live engine
    end-to-end against an existing ``run_dir`` (built by init-ledger).
    Supports ``--readonly`` (Phase D dry run) and
    ``--max-orders-per-day`` (§ 9 cap).

Subcommands deferred to subsequent PRs:
  * ``reconcile`` — invoke ``app.engine.live.reconcile`` post-force-flat.
  * ``emergency-flatten`` — manual operator path for the contaminated-account
    case in § 7.2 #6.

CLI exit codes:
  0  — success / pre-flight passed / start completed cleanly
  1  — pre-flight failed (halt) / start halted by max-orders / similar
  2  — operator error (bad args, missing files at init time)
  3  — start failed due to a runtime error (broker, IO)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from app.engine.live.pre_flight import (
    check_clean_tree,
    check_no_halt_flag,
    check_ntp_offset,
    check_run_state_intact,
    check_unexpected_position,
    check_yesterday_artifacts_valid,
    run_pre_flight,
)
from app.engine.live.run_ledger import (
    build_ledger,
    read_ledger,
    write_ledger,
)

logger = logging.getLogger(__name__)


def _git_head_sha(repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        timeout=5.0,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git rev-parse HEAD failed in {repo_root}: rc={proc.returncode} stderr={proc.stderr!r}"
        )
    sha = proc.stdout.strip()
    if not sha:
        raise RuntimeError(f"git rev-parse HEAD returned empty in {repo_root}")
    return sha


# ──────────────────────────── init-ledger subcommand ─────────────────


def cmd_init_ledger(args: argparse.Namespace) -> int:
    """Build a new live-run ledger and write it under ``--run-root/<run_id>/``.

    Refuses to start with a dirty source tree — see § 9. The clean-tree
    check is the gate that makes ``code_sha`` (set to git HEAD) the
    *actual* identity of the running code, not just a "close enough"
    hint.
    """
    repo_root: Path = args.repo_root.resolve()
    scope_paths = [Path(p) for p in args.clean_tree_scope]

    clean = check_clean_tree(scope_paths, repo_root=repo_root)
    if not clean.passed:
        print(f"[INIT-LEDGER] dirty-tree halt: {clean.detail}", file=sys.stderr)
        return 1

    code_sha = _git_head_sha(repo_root)

    if args.live_config_json:
        try:
            live_config = json.loads(args.live_config_json)
        except json.JSONDecodeError as exc:
            print(
                f"[INIT-LEDGER] --live-config-json is not valid JSON: {exc}",
                file=sys.stderr,
            )
            return 2
        if not isinstance(live_config, dict):
            print(
                f"[INIT-LEDGER] --live-config-json must be a JSON object, got {type(live_config).__name__}",
                file=sys.stderr,
            )
            return 2
    else:
        live_config = {}

    try:
        ledger = build_ledger(
            code_sha=code_sha,
            strategy_spec_path=args.strategy_spec_path,
            qc_audit_copy_path=args.qc_audit_copy_path,
            qc_cloud_backtest_id=args.qc_cloud_backtest_id,
            account_id=args.account_id,
            start_date_ms=args.start_date_ms,
            live_config=live_config,
        )
    except FileNotFoundError as exc:
        print(f"[INIT-LEDGER] missing input: {exc}", file=sys.stderr)
        return 2

    run_dir = args.run_root / ledger.run_id
    if run_dir.exists() and not args.force:
        print(
            f"[INIT-LEDGER] run directory already exists: {run_dir}. "
            f"Pass --force to overwrite (rare — usually means re-running with identical inputs).",
            file=sys.stderr,
        )
        return 2

    write_ledger(run_dir / "run_ledger.json", ledger)
    print(f"[INIT-LEDGER] wrote {run_dir}/run_ledger.json (run_id={ledger.run_id})")
    return 0


# ──────────────────────────── pre-flight subcommand ──────────────────


@dataclass(frozen=True)
class _PositionStub:
    symbol: str
    quantity: float


@dataclass(frozen=True)
class _PositionsStubSnapshot:
    positions: list


def _load_positions_snapshot(json_path: Path) -> _PositionsStubSnapshot:
    """Load a positions snapshot JSON for the standalone pre-flight subcommand.

    Expected JSON shape: ``{"positions": [{"symbol": "SPY", "quantity": 200}, ...]}``.
    Live runner integrations don't go through this helper — they pass an
    ``IbkrPositionsSnapshot`` straight into ``check_unexpected_position``.
    """
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    positions = [_PositionStub(symbol=p["symbol"], quantity=p["quantity"]) for p in payload["positions"]]
    return _PositionsStubSnapshot(positions=positions)


def cmd_pre_flight(args: argparse.Namespace) -> int:
    """Run all morning-gate halt checks. Non-zero exit on any halt."""
    repo_root: Path = args.repo_root.resolve()
    scope_paths = [Path(p) for p in args.clean_tree_scope]

    checks = []
    checks.append(check_clean_tree(scope_paths, repo_root=repo_root))
    checks.append(check_run_state_intact(args.run_dir))
    checks.append(check_no_halt_flag(args.run_dir))

    if args.skip_ntp:
        print("[PRE-FLIGHT] skipping NTP check (--skip-ntp)")
    else:
        checks.append(
            check_ntp_offset(
                server=args.ntp_server,
                max_offset_seconds=args.ntp_max_offset_seconds,
                timeout_seconds=args.ntp_timeout_seconds,
            )
        )

    if args.positions_json is not None:
        try:
            snapshot = _load_positions_snapshot(args.positions_json)
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            print(
                f"[PRE-FLIGHT] --positions-json could not be read as a positions snapshot: {exc}",
                file=sys.stderr,
            )
            return 2
        checks.append(check_unexpected_position(snapshot, expected_symbol=args.expected_symbol))
    else:
        print(
            "[PRE-FLIGHT] skipping unexpected-position check "
            "(no --positions-json supplied; the live runner enforces this when connected to IB)"
        )

    if args.yesterday_day_n is not None:
        checks.append(
            check_yesterday_artifacts_valid(
                run_dir=args.run_dir,
                qc_dir=args.qc_dir,
                docs_dir=args.docs_dir,
                yesterday_day_n=args.yesterday_day_n,
            )
        )

    all_passed, results = run_pre_flight(checks)
    for r in results:
        marker = "OK " if r.passed else "FAIL"
        print(f"[PRE-FLIGHT] {marker} {r.name}: {r.detail}")
    if not all_passed:
        print("[PRE-FLIGHT] HALT — at least one check failed; refusing to place orders today.")
        return 1
    print("[PRE-FLIGHT] all checks passed; runner may proceed.")
    return 0


# ──────────────────────────── start subcommand ───────────────────────


def _live_config_from_ledger(payload: dict) -> LiveConfig:  # noqa: F821
    """Build a LiveConfig from the ledger's serialized live_config dict.

    The ledger's ``live_config`` is the JSON form of the same fields
    LiveConfig carries; this round-trips them so the runtime sees the
    same values that went into ``run_id``. Unknown keys are rejected
    (they'd indicate the ledger was written with a newer schema than
    this code understands — refuse rather than silently drop).

    Empty payload ⇒ all-defaults LiveConfig (the canonical no-op case
    where the operator didn't pass --live-config-json at init-ledger).
    """
    from datetime import time

    from app.engine.live.config import LiveConfig

    if not payload:
        return LiveConfig()

    known_fields = {
        "symbol",
        "force_flat_at",
        "consolidator_period_min",
        "run_dir",
        "max_submit_latency_ms",
    }
    unknown = set(payload.keys()) - known_fields
    if unknown:
        raise ValueError(f"unknown live_config keys: {sorted(unknown)}")

    kwargs: dict = {}
    if "symbol" in payload:
        kwargs["symbol"] = str(payload["symbol"])
    if "force_flat_at" in payload:
        raw = payload["force_flat_at"]
        if raw is None:
            kwargs["force_flat_at"] = None
        elif isinstance(raw, str):
            # Accept "HH:MM" or "HH:MM:SS" — the canonical format
            # init-ledger writes via a JSON object.
            parts = raw.split(":")
            if len(parts) == 2:
                kwargs["force_flat_at"] = time(int(parts[0]), int(parts[1]))
            elif len(parts) == 3:
                kwargs["force_flat_at"] = time(int(parts[0]), int(parts[1]), int(parts[2]))
            else:
                raise ValueError(f"force_flat_at format must be HH:MM or HH:MM:SS, got {raw!r}")
        else:
            raise TypeError(f"force_flat_at must be string or null, got {type(raw).__name__}")
    if "consolidator_period_min" in payload:
        kwargs["consolidator_period_min"] = int(payload["consolidator_period_min"])
    if "run_dir" in payload:
        kwargs["run_dir"] = Path(str(payload["run_dir"]))
    if "max_submit_latency_ms" in payload:
        kwargs["max_submit_latency_ms"] = int(payload["max_submit_latency_ms"])

    return LiveConfig(**kwargs)


def cmd_start(args: argparse.Namespace) -> int:
    """Run the live engine end-to-end against an existing run directory.

    Reads the run ledger to recover identity (account_id, etc.), opens
    an IBKR connection (or accepts an injected broker for tests), and
    drives ``LiveEngine.run`` with the artifact writer integration
    pointed at ``--run-dir``. Honors ``--readonly`` (Phase D dry run —
    no actual broker submissions) and ``--max-orders-per-day`` (§ 9
    cap; default 4).

    Strategy import is dynamic so the operator can swap algorithms
    without editing this file: ``--strategy spy_ema_crossover`` resolves
    to ``app.engine.strategy.algorithms.spy_ema_crossover.SpyEmaCrossoverAlgorithm``
    by convention. Add new strategies by following the same naming
    convention.
    """
    from importlib import import_module

    from app.engine.live.halt import read_poisoned_flag
    from app.engine.live.live_engine import (
        LiveEngine,
        MaxOrdersPerDayExceeded,
    )

    # § 7.2 #4 refusal: a poisoned run cannot resume on its own
    # run_id. The flag is written intra-day by the LiveEngine when
    # broker-state divergence is detected; it stays in place until the
    # operator manually reconciles the account and starts a fresh
    # run_id. Surface the trigger and timestamp in the exit message
    # so the operator's next steps are obvious.
    try:
        poison = read_poisoned_flag(args.run_dir)
    except ValueError as exc:
        # Corrupted flag — treat as poisoned (refuse to start) rather
        # than silently ignore. The spec invariant is that
        # poisoned.flag is never the source of a clean restart.
        print(f"[START] poisoned.flag at {args.run_dir} is corrupted: {exc}", file=sys.stderr)
        return 1
    if poison is not None:
        print(
            f"[START] HALT — run is poisoned ({poison.trigger.value} at "
            f"{poison.halted_at_ms}ms UTC). § 7.2 #5: a fresh run_id is "
            f"required after manual account reconciliation.",
            file=sys.stderr,
        )
        return 1

    ledger_path = args.run_dir / "run_ledger.json"
    if not ledger_path.exists():
        print(f"[START] missing run_ledger.json at {ledger_path}", file=sys.stderr)
        return 2
    try:
        ledger = read_ledger(ledger_path)
    except (OSError, ValueError) as exc:
        print(f"[START] could not parse run_ledger.json: {exc}", file=sys.stderr)
        return 2

    try:
        module = import_module(f"app.engine.strategy.algorithms.{args.strategy}")
    except ImportError as exc:
        print(f"[START] could not import strategy module {args.strategy!r}: {exc}", file=sys.stderr)
        return 2
    # Convention: snake_case module name → PascalCase class with
    # "Algorithm" suffix. e.g. spy_ema_crossover → SpyEmaCrossoverAlgorithm.
    class_name = "".join(part.capitalize() for part in args.strategy.split("_")) + "Algorithm"
    strategy_cls = getattr(module, class_name, None)
    if strategy_cls is None:
        print(
            f"[START] strategy module {args.strategy!r} has no class {class_name!r} "
            f"(naming convention: snake_case module → PascalCase + 'Algorithm')",
            file=sys.stderr,
        )
        return 2
    strategy = strategy_cls()

    # Apply ledger.live_config so the runtime matches what was hashed
    # into run_id. Without this, code_sha + spec_hash + qc_audit_hash
    # would identify the run while the live config silently drifts to
    # LiveConfig defaults — the §10 identity guarantee is then a lie.
    # (CodeRabbit P2 from #186.)
    try:
        live_config = _live_config_from_ledger(ledger.live_config)
    except (TypeError, ValueError) as exc:
        print(
            f"[START] could not apply ledger.live_config to LiveConfig: {exc}",
            file=sys.stderr,
        )
        return 2

    # Test injection point: ``args.broker`` (set programmatically) lets
    # tests pass a FakeBroker without an IBKR client. Operator runs
    # always go through the IBKR client path below.
    broker = getattr(args, "broker", None)
    if broker is not None:
        engine = LiveEngine(
            None,
            live_config,
            broker=broker,
            output_dir=args.run_dir,
            account_id=ledger.account_id,
            readonly=args.readonly,
            max_orders_per_day=args.max_orders_per_day,
        )
    else:
        from app.broker.ibkr.client import IbkrClient

        client = IbkrClient()
        engine = LiveEngine(
            client,
            live_config,
            output_dir=args.run_dir,
            account_id=ledger.account_id,
            readonly=args.readonly,
            max_orders_per_day=args.max_orders_per_day,
        )

    bars_iter = getattr(args, "bars", None)
    print(
        f"[START] run_id={ledger.run_id} account={ledger.account_id} "
        f"readonly={args.readonly} max_orders_per_day={args.max_orders_per_day}"
    )
    try:
        asyncio.run(engine.run(strategy, bars=bars_iter))
    except MaxOrdersPerDayExceeded as exc:
        print(f"[START] HALT — max orders per day exceeded: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[START] runtime error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    print("[START] run completed cleanly")
    return 0


# ──────────────────────────── Argparse ───────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.engine.live.run",
        description="Run the SPY EMA crossover strategy against IBKR paper trading.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # init-ledger
    init = sub.add_parser(
        "init-ledger",
        help="Build run_ledger.json for a new live run.",
    )
    init.add_argument("--repo-root", type=Path, required=True, help="Repository root for git ops.")
    init.add_argument(
        "--clean-tree-scope",
        nargs="+",
        default=["PythonDataService", "references/qc-shadow"],
        help="Paths included in the dirty-tree refusal (relative to --repo-root).",
    )
    init.add_argument("--strategy-spec-path", type=Path, required=True)
    init.add_argument("--qc-audit-copy-path", type=Path, required=True)
    init.add_argument("--qc-cloud-backtest-id", required=True)
    init.add_argument("--account-id", required=True, help="IBKR account ID, e.g. DU1234567.")
    init.add_argument("--start-date-ms", type=int, required=True, help="int64 ms UTC of the run-start session.")
    init.add_argument(
        "--live-config-json",
        default=None,
        help="JSON object of resolved LiveConfig values (NOT raw env vars).",
    )
    init.add_argument(
        "--run-root",
        type=Path,
        default=Path("PythonDataService/artifacts/live_runs"),
        help="Parent directory under which the per-run-id directory is created.",
    )
    init.add_argument("--force", action="store_true", help="Overwrite an existing run directory.")
    init.set_defaults(func=cmd_init_ledger)

    # pre-flight
    pre = sub.add_parser(
        "pre-flight",
        help="Run morning-gate halt checks; non-zero exit on halt.",
    )
    pre.add_argument("--repo-root", type=Path, required=True)
    pre.add_argument(
        "--clean-tree-scope",
        nargs="+",
        default=["PythonDataService", "references/qc-shadow"],
    )
    pre.add_argument("--run-dir", type=Path, required=True, help="live_runs/<run_id>/ directory.")
    pre.add_argument("--qc-dir", type=Path, default=Path("artifacts/qc"))
    pre.add_argument(
        "--docs-dir",
        type=Path,
        default=Path("docs/references/reconciliations"),
    )
    pre.add_argument(
        "--yesterday-day-n",
        type=int,
        default=None,
        help="If set, verify yesterday's day-N reconciliation artifacts hash-match.",
    )
    pre.add_argument("--skip-ntp", action="store_true", help="Skip the NTP-offset check (offline / CI).")
    pre.add_argument("--ntp-server", default="pool.ntp.org")
    pre.add_argument("--ntp-max-offset-seconds", type=float, default=1.0)
    pre.add_argument("--ntp-timeout-seconds", type=float, default=5.0)
    pre.add_argument(
        "--positions-json",
        type=Path,
        default=None,
        help=(
            "Path to a JSON file with shape "
            '`{"positions": [{"symbol": "SPY", "quantity": 200}, ...]}`. '
            "When provided, exercises check_unexpected_position. The live runner passes "
            "this in directly from the IBKR connection."
        ),
    )
    pre.add_argument(
        "--expected-symbol",
        default="SPY",
        help="Symbol expected for the running strategy (long-only). Used by the position check.",
    )
    pre.set_defaults(func=cmd_pre_flight)

    # start
    start = sub.add_parser(
        "start",
        help="Run the live engine end-to-end against an existing run directory.",
    )
    start.add_argument("--run-dir", type=Path, required=True, help="live_runs/<run_id>/ directory built by init-ledger.")
    start.add_argument(
        "--strategy",
        default="spy_ema_crossover",
        help=(
            "Strategy module under app.engine.strategy.algorithms (snake_case). "
            "Class name is inferred (PascalCase + 'Algorithm')."
        ),
    )
    start.add_argument(
        "--readonly",
        action="store_true",
        help=(
            "Phase D dry-run mode: drain pending orders without calling broker.place_order. "
            "Decisions are still recorded; no fills come back; executions parquet stays empty."
        ),
    )
    start.add_argument(
        "--max-orders-per-day",
        type=int,
        default=4,
        help=(
            "§ 9 cap. Crossing this halts the run with exit 1. "
            "Default 4 (≤ 1 entry + 1 exit + 1 retry + 1 force-flat)."
        ),
    )
    start.set_defaults(func=cmd_start)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
