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
    ``--max-orders-per-day`` (§ 9 cap). Refuses if poisoned.flag
    exists for the run_dir (§ 7.2 #4).
  * ``emergency-flatten`` — manual operator path for the
    contaminated-account case in § 7.2 #6. Requires ``--confirm`` +
    ``--account``; logs every action to
    ``<run_dir>/emergency_flatten.log``.
  * ``pause`` / ``resume`` / ``stop`` — set the durable operator
    desired-state for a strategy_instance_id (PRD-A § 16.4 Resolution
    7 / PR-D). Writes ``desired_state.json`` under
    ``<artifacts-root>/live_state/<strategy_instance_id>/``; survives
    crash + reboot. ``start`` reads it: PAUSED boots paused, STOPPED
    refuses to start.

Subcommands deferred to subsequent PRs:
  * ``reconcile`` — invoke ``app.engine.live.reconcile`` post-force-flat.

CLI exit codes:
  0  — success / pre-flight passed / start completed cleanly
  1  — pre-flight failed (halt) / start halted by max-orders or fatal
       halt / poisoned.flag refusal / etc.
  2  — operator error (bad args, missing files at init time, missing
       --confirm on emergency-flatten, account mismatch)
  3  — start or emergency-flatten failed due to a runtime error
       (broker, IO)
  4  — indicator-state hydration failed under REQUIRE policy (B2
       dry-run gate); see indicator_state_hydration.json in run_dir
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from app.engine.live.live_state_sidecar import LiveStateEnvelope

from app.broker.runtime_snapshot import make_live_engine_verdict_provider
from app.engine.live.deploy import (
    DeployIOError,
    DeployParams,
    DirtyTreeError,
    GitUnavailableError,
    RunAlreadyExistsError,
    SizingPolicyMissingError,
    SpecOrAuditMissingError,
    UnknownLiveConfigKeyError,
    deploy_run,
)
from app.engine.live.pre_flight import (
    check_all_in_coexistence,
    check_clean_tree,
    check_no_halt_flag,
    check_ntp_offset,
    check_run_state_intact,
    check_unexpected_position,
    check_yesterday_artifacts_valid,
    run_pre_flight,
)
from app.engine.live.run_ledger import read_ledger

logger = logging.getLogger(__name__)


def _build_child_watchdog_factory(artifacts_root: Path, run_dir: Path):
    """PRD #619-B B5 follow-up — return a factory the LiveEngine calls
    to construct a configured ``ChildWatchdog``.

    Reads ``LIVE_RUNNER_DAEMON_BOOT_ID`` from the env (the host daemon
    sets this on every child spawn — see ``host_daemon._build_child_env``).
    When the env var is unset (CLI runs without the daemon), the
    factory returns a watchdog with ``expected_daemon_boot_id=None``,
    which skips the ``BOOT_ID_CHANGED`` check but still detects an
    expired lease.
    """
    import os as _os

    def _factory(
        *,
        block_submissions,
        persist_paused,
        disconnect_broker,
        request_engine_exit,
        aggregator=None,
        executor=None,
    ):
        from app.engine.live.child_watchdog import ChildWatchdog

        return ChildWatchdog(
            artifacts_root=artifacts_root,
            run_dir=run_dir,
            expected_daemon_boot_id=_os.environ.get("LIVE_RUNNER_DAEMON_BOOT_ID"),
            block_submissions=block_submissions,
            persist_paused=persist_paused,
            disconnect_broker=disconnect_broker,
            request_engine_exit=request_engine_exit,
            now_ms=lambda: int(time.time() * 1000),
            aggregator=aggregator,
            executor=executor,
        )

    return _factory


# ──────────────────────────── init-ledger subcommand ─────────────────


def cmd_init_ledger(args: argparse.Namespace) -> int:
    """Build a new live-run ledger and write it under ``--run-root/<run_id>/``.

    Thin CLI wrapper over :func:`app.engine.live.deploy.deploy_run` (ADR 0006):
    the deploy seam owns the dirty-tree gate, ``code_sha`` capture, ledger
    build/write, and idempotency; this maps its typed exceptions to the CLI's
    exit codes. Non-idempotent here (an existing run dir is an error, exit 2)
    so the CLI contract is unchanged.

    Persists ``--strategy-instance-id`` (UI-0) into the ledger (schema 1.1) so a
    fresh, pre-decision run has an O(1) ``run_id -> strategy_instance_id``
    mapping. It is NOT hashed into ``run_id``; omitting it records an empty
    binding (legacy / unknown).
    """
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

    params = DeployParams(
        repo_root=args.repo_root,
        strategy_spec_path=args.strategy_spec_path,
        qc_audit_copy_path=args.qc_audit_copy_path,
        qc_cloud_backtest_id=args.qc_cloud_backtest_id,
        account_id=args.account_id,
        start_date_ms=args.start_date_ms,
        run_root=args.run_root,
        live_config=live_config,
        strategy_instance_id=args.strategy_instance_id,
        strategy_key=args.strategy_key,
        clean_tree_scope=tuple(args.clean_tree_scope),
        force=args.force,
        idempotent=False,
    )
    try:
        result = deploy_run(params)
    except DirtyTreeError as exc:
        print(f"[INIT-LEDGER] dirty-tree halt: {exc}", file=sys.stderr)
        return 1
    except GitUnavailableError as exc:
        print(f"[INIT-LEDGER] {exc}", file=sys.stderr)
        return 1
    except SpecOrAuditMissingError as exc:
        print(f"[INIT-LEDGER] missing input: {exc}", file=sys.stderr)
        return 2
    except SizingPolicyMissingError as exc:
        print(f"[INIT-LEDGER] {exc}", file=sys.stderr)
        return 2
    except UnknownLiveConfigKeyError as exc:
        print(f"[INIT-LEDGER] {exc}", file=sys.stderr)
        return 2
    except DeployIOError as exc:
        print(f"[INIT-LEDGER] filesystem error: {exc}", file=sys.stderr)
        return 1
    except RunAlreadyExistsError as exc:
        print(
            f"[INIT-LEDGER] run directory already exists: {exc.run_dir}. "
            f"Pass --force to overwrite (rare — usually means re-running with identical inputs).",
            file=sys.stderr,
        )
        return 2

    print(f"[INIT-LEDGER] wrote {result.run_dir}/run_ledger.json (run_id={result.run_id})")
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
    # VCR-0001 / Phase 1 — surface the sizing-policy-present gate in the
    # manual pre-flight subcommand too. Reads the ledger directly (if
    # present) so the CLI shows the same verdict ``cmd_start`` will give
    # before the operator runs it. A run dir without a ledger is treated
    # as a legacy/pre-policy ledger and fails the gate (the operator must
    # redeploy with an explicit policy).
    _ledger_live_config: dict = {}
    _pre_ledger_path = args.run_dir / "run_ledger.json"
    if _pre_ledger_path.is_file():
        try:
            _ledger_live_config = json.loads(_pre_ledger_path.read_text(encoding="utf-8")).get("live_config") or {}
        except (OSError, json.JSONDecodeError):
            _ledger_live_config = {}
    from app.engine.live.pre_flight import check_sizing_policy_present

    checks.append(check_sizing_policy_present(_ledger_live_config))

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
        managed_symbols = (
            {s.strip() for s in args.managed_symbols.split(",") if s.strip()} if args.managed_symbols else None
        )
        checks.append(
            check_unexpected_position(
                snapshot,
                expected_symbol=args.expected_symbol,
                managed_symbols=managed_symbols,
            )
        )
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


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, shutdown_event: asyncio.Event) -> None:
    """Wire SIGINT and SIGTERM to set ``shutdown_event``.

    Linux/container only — ``loop.add_signal_handler`` is not
    implemented on Windows's default event loop. The intended
    execution host is the polygon-data-service container (Linux), so
    on Windows we log a warning and fall through; the operator's
    Ctrl-C will surface as a ``KeyboardInterrupt`` which ``cmd_start``
    catches via its generic exception path (still recorded, just not
    a graceful flatten).
    """

    def _handle(sig_value: int) -> None:
        logger.info(
            "Received signal %d; setting shutdown_event for graceful exit",
            sig_value,
            extra={"step": "9"},
        )
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle, sig)
        except NotImplementedError:
            logger.warning(
                "Signal handler for %s not supported on this event loop "
                "(Windows host?); graceful shutdown via this signal disabled.",
                sig.name,
            )


def _resolve_recovery_broker(broker, client):
    """Return the broker to use for cmd_start's recovery flatten.

    The returned broker is the same instance the engine ran with — both
    the test path (FakeBroker injected via ``args.broker``) and the
    production path (``IbkrBrokerAdapter`` constructed in ``cmd_start``
    and passed to ``LiveEngine(broker=...)``) preserve
    ``IbkrBrokerAdapter._owned_order_ids``. Cancelling on a fresh
    adapter (the prior behavior) would skip the runner's in-flight
    orders entirely and leave them working while liquidations also fly,
    yielding double-state on the account.

    Returns ``None`` when:
      * no broker is in scope (shouldn't happen with the post-refactor
        ``cmd_start``, but defensive); or
      * the client has disconnected — recovery flatten requires a live
        broker session, and a stale client makes ``fetch_positions`` /
        ``cancel_open_orders`` calls non-deliverable.

    When ``None`` is returned the operator is told to run
    ``emergency-flatten --confirm`` manually.
    """
    if broker is None:
        return None
    if client is not None and not client.is_connected():
        return None
    return broker


def _is_recovery_readonly(args, client) -> bool:
    """True if the recovery-flatten path should run in readonly mode.

    The CLI ``--readonly`` flag (``args.readonly``) and the
    ``IBKR_READONLY`` env var (which surfaces as
    ``client.settings.readonly``) can diverge: ``IbkrClient()`` reads
    settings from env only, so a CLI ``--readonly`` does NOT
    propagate to ``client.settings``. The recovery path must respect
    either signal — operator intent on the command line, or the
    deployment-wide default. Safer-of-the-two on each path.
    """
    args_readonly = bool(getattr(args, "readonly", False))
    if args_readonly:
        return True
    if client is None:
        return False
    settings = getattr(client, "settings", None)
    return bool(getattr(settings, "readonly", False))


def _append_live_state_submitted_order(
    live_state_path: Path,
    *,
    client_order_id: str,
    perm_id: int | None,
    order_id: int,
    status: str,
    symbol: str,
    seed_envelope: LiveStateEnvelope | None = None,
) -> None:
    """Append one submitted-order fingerprint to the live-state sidecar."""
    from app.engine.live.live_state_sidecar import LiveStateSidecarRepo

    repo = LiveStateSidecarRepo(live_state_path)
    existing = repo.read()
    if existing is None:
        if seed_envelope is None:
            return
        existing = seed_envelope

    submitted_orders = dict(existing.submitted_orders)
    submitted_orders[client_order_id] = {
        "perm_id": perm_id,
        "order_id": order_id,
        "status": status,
        "symbol": symbol,
    }
    known_perm_ids = list(existing.known_perm_ids)
    if perm_id is not None and perm_id not in known_perm_ids:
        known_perm_ids.append(perm_id)

    repo.write(
        existing.model_copy(
            update={
                "submitted_orders": submitted_orders,
                "known_perm_ids": known_perm_ids,
                "last_artifact_flush_ms": int(time.time() * 1000),
            }
        )
    )


# Recovery flatten runs after the engine's order-event stream has stopped, so
# the synchronous place_order ack is its only chance to capture the permId the
# next same-account relaunch needs (see _append_live_state_submitted_order and
# halt.check_outside_mutation). Wait briefly for IBKR to assign it. permId
# normally arrives in well under 1s; 2s tolerates a degraded connection without
# stalling the crash-recovery path.
_RECOVERY_PERM_ID_WAIT_S = 2.0


async def _recovery_flatten(
    broker,
    *,
    readonly: bool = False,
    live_state_path: Path | None = None,
    live_state_seed: LiveStateEnvelope | None = None,
    bot_order_namespace: str | None = None,
) -> int:
    """Best-effort cancel + flatten for the cmd_start unhandled-exception path.

    Different from ``cmd_emergency_flatten``: no ``--confirm`` gate,
    no account-id match check (we trust the broker we're already
    connected to). Different from ``LiveEngine._shutdown_flatten``:
    that runs inside ``engine.run`` with the engine's portfolio in
    scope; this runs in cmd_start's exception path where the engine's
    portfolio is no longer reachable, so we re-fetch positions from
    the broker.

    When ``readonly`` is True, the function enumerates positions and
    logs what *would* have been liquidated but does NOT call
    ``cancel_open_orders`` and does NOT call ``place_order``. This
    preserves the ``IBKR_READONLY=true`` contract documented on
    ``IbkrSettings.readonly`` (``config.py``: "when True, every call
    to ``place_paper_order`` raises ``OrderRefusedError`` before any
    contract is built") on the unhandled-exception path. Operators
    can still run ``emergency-flatten --confirm`` afterwards if the
    detected positions need cleanup.

    Returns the number of liquidation orders submitted, or — in
    readonly mode — the number of non-zero positions detected.
    Per-position place_order failures are logged but don't abort the
    loop — every remaining position still gets an attempt.

    VCR-0019: positions are re-fetched after ``cancel_open_orders``
    AND once more per-symbol immediately before each ``place_order``
    so a strategy fill that landed after the initial enumeration does
    not drive a duplicate liquidation (the engine's bar loop has
    stopped; only the broker is authoritative).

    VCR-0020: every spec carries a deterministic ``order_ref`` so a
    real-broker ``requires_durable_submit=True`` adapter accepts it.
    ``bot_order_namespace`` is sourced from the explicit parameter
    first, then from ``live_state_seed.bot_order_namespace``. Callers
    that supply neither (legacy / fake-broker tests) get unstamped
    specs — the ``place_paper_order`` invariant only fires for the
    real-broker path.
    """
    from datetime import UTC, datetime

    from app.broker.ibkr.models import IbkrOrderSpec
    from app.engine.live.order_identity import build_order_ref, mint_intent_id

    resolved_namespace = bot_order_namespace
    if resolved_namespace is None and live_state_seed is not None:
        resolved_namespace = live_state_seed.bot_order_namespace

    snapshot = await broker.fetch_positions()

    if readonly:
        detected = 0
        for pos in snapshot.positions:
            qty_signed = float(pos.quantity)
            if qty_signed == 0:
                continue
            action = "SELL" if qty_signed > 0 else "BUY"
            logger.info(
                "Recovery flatten (readonly): would have submitted %s %s qty=%s",
                action,
                pos.symbol,
                abs(qty_signed),
                extra={"step": "8"},
            )
            detected += 1
        return detected

    # Phase 5C / VCR-0002 — managed cancel-confirm timeout in the
    # recovery-flatten path. PRD §5C: do NOT liquidate when the cancel
    # confirms can't return within the window; surface as a fatal halt
    # so the operator inspects rather than racing cancels against the
    # immediately-following market liquidations.
    from app.engine.live.live_engine import (
        CANCEL_CONFIRM_TIMEOUT_S,
        CancelConfirmTimeoutHaltError,
    )

    try:
        cancelled = await asyncio.wait_for(broker.cancel_open_orders(), timeout=CANCEL_CONFIRM_TIMEOUT_S)
    except TimeoutError as exc:
        logger.error(
            "cancel_open_orders timed out during recovery flatten; refusing to liquidate",
            extra={"step": "8", "timeout_s": CANCEL_CONFIRM_TIMEOUT_S},
        )
        raise CancelConfirmTimeoutHaltError(timeout_s=CANCEL_CONFIRM_TIMEOUT_S) from exc
    except Exception:
        logger.exception(
            "cancel_open_orders failed during recovery flatten",
            extra={"step": "8"},
        )
        cancelled = []
    if cancelled:
        logger.info(
            "Recovery flatten cancelled %d open order(s)",
            len(cancelled),
            extra={"step": "8"},
        )

    # VCR-0019 — re-fetch after cancel_open_orders. The original snapshot was
    # taken before the cancel; if the engine's last strategy SELL fill landed
    # at the broker in that window, the stale snapshot would drive a
    # duplicate liquidation and put the account net-short. The post-cancel
    # snapshot is authoritative for the liquidation loop.
    snapshot = await broker.fetch_positions()

    liquidated = 0
    for pos in snapshot.positions:
        qty_signed = float(pos.quantity)
        if qty_signed == 0:
            continue

        # VCR-0019 — guard the loop body too. Even after the post-cancel
        # snapshot above, a fill may land while we iterate. One more refresh
        # per symbol closes the residual race; on the typical 0-1 position
        # account this is at most a single extra round-trip.
        fresh = await broker.fetch_positions()
        fresh_qty = next(
            (float(p.quantity) for p in fresh.positions if p.symbol == pos.symbol),
            0.0,
        )
        if fresh_qty == 0:
            logger.info(
                "Recovery flatten skipped %s — broker now reports flat (race resolved before submit)",
                pos.symbol,
                extra={"step": "8"},
            )
            continue
        qty_signed = fresh_qty
        action = "SELL" if qty_signed > 0 else "BUY"
        order_ref = build_order_ref(resolved_namespace, mint_intent_id()) if resolved_namespace else None
        spec = IbkrOrderSpec(
            symbol=pos.symbol,
            sec_type=pos.sec_type,
            action=action,
            quantity=abs(qty_signed),
            order_type="MKT",
            time_in_force="DAY",
            confirm_paper=True,
            client_order_id=f"recovery-flatten-{pos.symbol}-{int(datetime.now(UTC).timestamp() * 1000)}",
            order_ref=order_ref,
        )
        try:
            # Wait for permId so the durable fingerprint below is recognizable
            # on relaunch — the engine's event stream is already stopped here.
            ack = await broker.place_order(spec, perm_id_wait_s=_RECOVERY_PERM_ID_WAIT_S)
            logger.info(
                "Recovery flatten liquidated %s qty=%s order_id=%s",
                pos.symbol,
                qty_signed,
                ack.order_id,
                extra={"step": "8"},
            )
            if live_state_path is not None and spec.client_order_id is not None:
                if ack.perm_id is None:
                    # No permId means the relaunch guard has nothing stable to
                    # match the replayed recovery fill against; record the
                    # fingerprint anyway, but flag the gap for the operator.
                    logger.warning(
                        "Recovery flatten for %s got no permId within %.1fs; "
                        "same-account relaunch may flag this fill as an outside "
                        "mutation — verify via emergency-flatten before restart",
                        pos.symbol,
                        _RECOVERY_PERM_ID_WAIT_S,
                        extra={"step": "8"},
                    )
                try:
                    _append_live_state_submitted_order(
                        live_state_path,
                        client_order_id=spec.client_order_id,
                        perm_id=ack.perm_id,
                        order_id=ack.order_id,
                        status=ack.status,
                        symbol=ack.symbol,
                        seed_envelope=live_state_seed,
                    )
                except Exception:
                    logger.exception(
                        "Recovery flatten could not record submitted order fingerprint",
                        extra={"step": "8"},
                    )
            liquidated += 1
        except Exception:
            logger.exception(
                "Recovery flatten place_order failed for %s",
                pos.symbol,
                extra={"step": "8"},
            )
    return liquidated


def _lookup_sizing_surface(strategy_key: str) -> str | None:
    """Resolve the strategy's registered ``sizing_surface`` (ADR 0009 § 6).

    ``cmd_start``'s ``--strategy`` arg is the algorithm **module** name
    (``import_module(f"app.engine.strategy.algorithms.{strategy}")``), but
    the registry is keyed by the operator-visible **registration name**
    (e.g. module ``spy_ema_crossover_options`` is registered as
    ``ema_crossover_options``). So we try the exact module name first and a
    ``spy_``-prefix-stripped form second — that covers every existing
    divergence today. A future module that breaks this rule will surface
    here as a ``None`` lookup; the fail-fast quietly disables for that
    strategy (same behaviour as legacy/test runs) until the lookup is
    extended.

    Tolerates an unregistered ``strategy_key`` (returns ``None`` so the
    fail-fast in LivePortfolio doesn't fire on legacy/test runs).
    """
    try:
        from app.routers.engine import _STRATEGY_REGISTRY  # local import: lazy
    except Exception:
        return None
    # VCR-0004 / Phase 2 — the registry is keyed by module name now, so the
    # legacy ``removeprefix("spy_")`` workaround is gone.
    reg = _STRATEGY_REGISTRY.get(strategy_key)
    return getattr(reg, "sizing_surface", None) if reg is not None else None


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

    from app.engine.live.config import LIVE_CONFIG_LEDGER_KEYS, LiveConfig

    if not payload:
        return LiveConfig()

    # VCR-0001 / Phase 1 — share the allow-list with the deploy-boundary
    # schema validator so any future sibling key is added in exactly one
    # place. CodeRabbit P2 review comment on PR #519.
    unknown = set(payload.keys()) - LIVE_CONFIG_LEDGER_KEYS
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
    if "sizing" in payload:
        from app.engine.execution.order_sizer import parse_sizing_policy

        raw = payload["sizing"]
        if raw is None:
            kwargs["sizing"] = None
        else:
            kwargs["sizing"] = parse_sizing_policy(raw)
    if "reconciliation_timing_policy" in payload:
        # ADR 0014 §6 — round-trip through the Pydantic model so a
        # malformed ledger fails fast at run start, not when the
        # publisher tries to construct the policy.
        from app.schemas.broker_activity import ReconciliationTimingPolicy

        raw = payload["reconciliation_timing_policy"]
        if raw is None:
            kwargs["reconciliation_timing_policy"] = None
        else:
            kwargs["reconciliation_timing_policy"] = (
                ReconciliationTimingPolicy.model_validate(raw).model_dump()
            )

    return LiveConfig(**kwargs)


def _make_ibkr_client(spec_client_id: int | None):
    """Construct the IBKR client for a live ``start``, pinning the Gateway
    clientId the strategy spec declares (PRD-A §16.3 isolation invariant).

    When ``spec_client_id`` is set, the per-strategy clientId overrides the
    env/default so two strategies never collide on one Gateway. When the
    spec omits it (``None``), fall back to ``IbkrSettings``' env/default
    clientId. ``IbkrClient()`` does not connect at construction, so this is
    safe to build before the pre-flight gates run.
    """
    from app.broker.ibkr.client import IbkrClient

    if spec_client_id is None:
        return IbkrClient()
    from app.broker.ibkr.config import get_settings

    settings = get_settings().model_copy(update={"client_id": spec_client_id})
    return IbkrClient(settings)


def _build_live_state_writer(
    *,
    strategy_instance_id: str,
    run_id: str,
    client: object | None,
    artifacts_root: Path,
):  # -> Callable[[LivePortfolio, int], None] | None  (lazy types to keep imports light)
    """Construct the sidecar-write callable LiveEngine invokes after each bar.

    Returns ``None`` when no IBKR client is available — the replay /
    test path. The callable snapshots position state from the portfolio
    and writes the 12-field envelope under the canonical
    ``artifacts/live_state/<strategy_instance_id>/live_state.json``
    path. Failures are caught inside the engine wrapper so a sidecar
    I/O hiccup doesn't crash the bar loop.
    """
    if client is None:
        return None
    # Test stubs may not carry a settings.client_id; without one we
    # cannot populate the envelope's ib_client_id field, so skip the
    # sidecar entirely.
    settings = getattr(client, "settings", None)
    raw_client_id = getattr(settings, "client_id", None) if settings is not None else None
    if raw_client_id is None:
        return None
    from app.engine.live.live_state_sidecar import (
        LiveStateEnvelope,
        LiveStateSidecarRepo,
        stable_live_state_path,
    )

    bot_order_namespace = f"learn-ai/{strategy_instance_id}/v1"
    ib_client_id = int(raw_client_id)
    repo = LiveStateSidecarRepo(stable_live_state_path(artifacts_root, strategy_instance_id))

    # ADR 0009 § 11 — bounded ring-buffer for the per-trade audit list. Keeps
    # the sidecar size predictable on a long-running bot; the cockpit only
    # renders the most recent rows anyway.
    SIZING_AUDIT_CAP = 200

    def _write(portfolio, bar_close_ms: int) -> None:
        existing = repo.read()
        # ADR 0009 § 14 + PR6 reviewer fix — the live-state sidecar is keyed
        # by ``strategy_instance_id``, not ``run_id``. When the operator
        # re-deploys the same instance with a fresh ``run_id``, the prior
        # run's audit rows linger on disk and would otherwise pollute the
        # new Sizing card. Discard them on run_id mismatch; honest empty
        # beats stale rows from a different policy.
        if existing is not None and existing.run_id != run_id:
            prior_audit: list[dict] = []
        else:
            prior_audit = existing.sizing_resolutions if existing is not None else []
        new_audit = list(getattr(portfolio, "sizing_resolutions", []))
        # Merge the prior-flush audit rows with whatever the portfolio
        # captured since, then keep only the last SIZING_AUDIT_CAP. The
        # portfolio's in-memory list resets per process so it doesn't grow
        # unbounded across multi-day runs; the sidecar is the persistent
        # home.
        combined_audit = (prior_audit + new_audit)[-SIZING_AUDIT_CAP:]
        envelope = LiveStateEnvelope(
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            bot_order_namespace=bot_order_namespace,
            ib_client_id=ib_client_id,
            pending_intents=existing.pending_intents if existing is not None else [],
            submitted_orders=existing.submitted_orders if existing is not None else {},
            known_perm_ids=existing.known_perm_ids if existing is not None else [],
            known_exec_ids=existing.known_exec_ids if existing is not None else [],
            last_processed_bar_ms=bar_close_ms,
            last_artifact_flush_ms=int(time.time() * 1000),
            expected_position_by_symbol={sym: int(pos.quantity) for sym, pos in portfolio.positions.items()},
            sizing_resolutions=combined_audit,
            poisoned_reason=existing.poisoned_reason if existing is not None else None,
        )
        repo.write(envelope)
        # PR6 reviewer fix — only drain the portfolio's buffer after the
        # write succeeded. If repo.write raised, the rows stay in memory and
        # the next flush retries them; clearing pre-write would have lost
        # them on a transient sidecar I/O failure.
        if hasattr(portfolio, "sizing_resolutions"):
            portfolio.sizing_resolutions.clear()

    return _write


def _build_live_state_seed_envelope(
    *,
    strategy_instance_id: str,
    run_id: str,
    client: object | None,
    last_processed_bar_ms: int,
) -> LiveStateEnvelope | None:
    """Build the minimal envelope recovery-flatten can seed before first flush."""
    if client is None:
        return None
    settings = getattr(client, "settings", None)
    raw_client_id = getattr(settings, "client_id", None) if settings is not None else None
    if raw_client_id is None:
        return None

    from app.engine.live.live_state_sidecar import LiveStateEnvelope

    return LiveStateEnvelope(
        strategy_instance_id=strategy_instance_id,
        run_id=run_id,
        bot_order_namespace=f"learn-ai/{strategy_instance_id}/v1",
        ib_client_id=int(raw_client_id),
        last_processed_bar_ms=max(1, int(last_processed_bar_ms)),
        last_artifact_flush_ms=int(time.time() * 1000),
    )


def _read_owned_perm_ids(live_state_path: Path) -> set[int]:
    """Load durable bot-owned permIds from the live-state sidecar."""
    from app.engine.live.live_state_sidecar import LiveStateSidecarRepo

    envelope = LiveStateSidecarRepo(live_state_path).read()
    if envelope is None:
        return set()
    return {int(perm_id) for perm_id in envelope.known_perm_ids}


def _build_broker_snapshot_from_ibkr(
    open_orders: list, executions: list
) -> object:
    """Map ``IbkrOpenOrder`` + ``IbkrOrderEvent`` into the classifier's pure
    ``BrokerSnapshot`` shape (ADR-0008 §5).

    Pure transform — no broker calls. The caller has already synchronized the
    broker caches via ``list_open_orders`` + ``executions_for_reconnect_recovery``
    so the empty-cache-defeats-reconciliation gate (Acceptance Gate #2) is
    satisfied.

    Quantity convention on executions: IBKR's ``Fill.execution.side`` is
    ``"BOT"`` / ``"SLD"``. We carry the **signed** quantity (positive for buys,
    negative for sells) so the classifier's sign-aware comparisons read
    correctly.
    """
    from app.engine.live.reconciliation_classifier import (
        BrokerExecutionView,
        BrokerOrderView,
        BrokerSnapshot,
    )

    order_views = tuple(
        BrokerOrderView(
            order_ref=getattr(o, "order_ref", None),
            perm_id=getattr(o, "perm_id", None),
            order_id=getattr(o, "order_id", None),
            status=getattr(o, "status", None),
            symbol=getattr(o, "symbol", None),
            remaining=float(getattr(o, "remaining", 0.0) or 0.0),
            filled=float(getattr(o, "cumulative_filled", 0.0) or 0.0),
        )
        for o in open_orders
    )
    exec_views = tuple(
        BrokerExecutionView(
            order_ref=getattr(e, "order_ref", None),
            perm_id=getattr(e, "perm_id", None),
            exec_id=getattr(e, "exec_id", None),
            symbol=getattr(e, "symbol", None),
            quantity=(
                float(getattr(e, "fill_quantity", 0.0) or 0.0)
                * (-1.0 if getattr(e, "side", None) == "SELL" else 1.0)
            ),
        )
        for e in executions
        if getattr(e, "event_type", None) == "fill"
    )
    return BrokerSnapshot(open_orders=order_views, executions=exec_views)


def _resolve_prior_run_dir(
    *, current_run_dir: Path, strategy_instance_id: str, current_created_ms: int
) -> Path | None:
    """Find the most-recent prior run dir for the same ``strategy_instance_id``.

    Scans siblings under ``current_run_dir.parent`` (the run root) for
    ``run_ledger.json`` files whose ``strategy_instance_id`` matches and whose
    ``created_at_ms`` strictly precedes ``current_created_ms``. Returns the
    newest such dir, or ``None`` when no eligible prior run exists.

    Silently tolerates unreadable / malformed sibling ledgers — a corrupt
    sibling cannot block this run's reconciliation; the classifier already
    treats absent prior-run evidence as empty.
    """
    parent = current_run_dir.parent
    if not parent.exists():
        return None
    best: tuple[int, Path] | None = None
    for sibling in parent.iterdir():
        if not sibling.is_dir() or sibling == current_run_dir:
            continue
        ledger_path = sibling / "run_ledger.json"
        if not ledger_path.exists():
            continue
        try:
            payload = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("strategy_instance_id") != strategy_instance_id:
            continue
        created_ms = payload.get("created_at_ms")
        if not isinstance(created_ms, int) or created_ms >= current_created_ms:
            continue
        if best is None or created_ms > best[0]:
            best = (created_ms, sibling)
    return best[1] if best is not None else None


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
    import os as _os
    from importlib import import_module

    from app.engine.live.account_identity import (
        AccountIdentityMismatchError,
        InvalidAccountIdError,
    )
    from app.engine.live.artifacts import DECISION_COLUMNS, resolve_decision_columns
    from app.engine.live.halt import FatalHaltError, read_poisoned_flag
    from app.engine.live.live_engine import (
        LiveEngine,
        MaxOrdersPerDayExceeded,
    )

    _AccountIdentityError = (AccountIdentityMismatchError, InvalidAccountIdError)
    from app.engine.live.run_logging import configure_run_logging
    from app.engine.live.run_status import now_ms, write_run_status
    from app.engine.strategy.spec import load_spec_from_path
    from app.schemas.live_runs import ExitReason, RunStatusSidecar

    def _record_poison_refusal() -> None:
        # Make the poison refusal legible to the console: record a terminal
        # `poisoned` status so the "Why It Stopped" panel shows "fresh run_id
        # required" instead of "ended cleanly" / blank.
        #
        # Preserve an existing status ONLY when it already explains a real
        # failure (e.g. fatal_halt — typically the very cause of the poison).
        # A clean-stop status is CONTRADICTED by the poison flag and must be
        # overwritten: MARK_POISONED writes poisoned.flag AND sets the bar
        # loop's shutdown_event (live_engine §"STOP / MARK_POISONED"), so the
        # run exits gracefully as `keyboard_interrupt`; skipping over that would
        # leave the UI showing the poisoned run as cleanly stopped.
        _EXPLANATORY_REASONS = {
            ExitReason.fatal_halt.value,
            ExitReason.exception.value,
            ExitReason.max_orders_exceeded.value,
            ExitReason.recovery_flatten.value,
            ExitReason.poisoned.value,
        }
        status_path = args.run_dir / "run_status.json"
        if status_path.exists():
            try:
                existing_reason = json.loads(status_path.read_text(encoding="utf-8")).get("exit_reason")
            except (OSError, ValueError):
                existing_reason = None
            if existing_reason in _EXPLANATORY_REASONS:
                return
        write_run_status(
            args.run_dir,
            RunStatusSidecar(
                run_id=args.run_dir.name,
                started_at_ms=now_ms(),
                last_update_ms=now_ms(),
                ended_at_ms=now_ms(),
                exit_code=1,
                exit_reason=ExitReason.poisoned,
                host_pid=_os.getpid(),
            ),
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
        _record_poison_refusal()
        return 1
    if poison is not None:
        print(
            f"[START] HALT — run is poisoned ({poison.trigger.value} at "
            f"{poison.halted_at_ms}ms UTC). § 7.2 #5: a fresh run_id is "
            f"required after manual account reconciliation.",
            file=sys.stderr,
        )
        _record_poison_refusal()
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

    # VCR-P3-Q / Phase 6D — rerun the halt.flag pre-flight at start time.
    # A halt.flag persisted from yesterday's session must refuse the start
    # even if the operator already ran ``cmd_pre_flight`` (which they might
    # not run on every restart). The flag is observed-only here; the
    # operator must remove it after reviewing the reason.
    _halt_check = check_no_halt_flag(args.run_dir)
    if not _halt_check.passed:
        print(
            f"[START] HALT — halt.flag present at {args.run_dir}: {_halt_check.detail}. "
            "Review the prior-session halt cause before clearing the flag and restarting.",
            file=sys.stderr,
        )
        return 2

    # VCR-0001 / Phase 1 — refuse to start a pre-policy ledger (no explicit
    # ``live_config.sizing``). Mirrors the deploy-boundary refusal so a legacy
    # ledger that pre-dates ADR 0009 cannot enter the runtime through this
    # path. There is no ``--allow-pre-policy-sizing`` override: ``live_config``
    # is hashed into ``run_id``, so a start-time effective-sizing change would
    # make the identity fingerprint dishonest. The read-only cockpit / Sizing-
    # card path still loads legacy ledgers via ``_live_config_from_ledger``.
    ledger_live_config = ledger.live_config if isinstance(ledger.live_config, dict) else {}
    if ledger_live_config.get("sizing") is None:
        print(
            "[START] HALT — live_config.sizing is missing from the ledger "
            "(pre-policy / VCR-0001). Phase 1 / ADR 0009 requires every new "
            "run to carry an explicit sizing policy. Redeploy with an "
            "explicit policy (Safe canary: "
            "{'sizing': {'kind': 'FixedShares', 'value': 1}}).",
            file=sys.stderr,
        )
        return 2

    # Foot-gun guard (#416): the algorithm is imported purely from --strategy,
    # while the ledger's spec/QC-audit pin the reconciliation target. A
    # mismatched --strategy would silently run a *different* algorithm against
    # a ledger reconciled to a different QC backtest. When the ledger records
    # the intended algorithm module (strategy_key, schema 1.2+), reject any
    # inconsistent --strategy up-front. Empty strategy_key (legacy ledger) is
    # unguarded — preserves existing runs that predate the field.
    if ledger.strategy_key and args.strategy != ledger.strategy_key:
        print(
            f"[START] --strategy {args.strategy!r} does not match the ledger's "
            f"strategy_key {ledger.strategy_key!r}. The ledger is reconciled to a "
            f"specific algorithm; starting a different one breaks the three-way "
            f"reconciliation guarantee. Start with --strategy {ledger.strategy_key!r}.",
            file=sys.stderr,
        )
        return 2

    # Attach file + console logging keyed off the run-dir. Done after
    # the poisoned-flag refusal and ledger read so a misconfigured run
    # never creates a fresh live.log under a poisoned run_dir.
    log_path = configure_run_logging(args.run_dir)
    logger.info(
        "Run logging attached: %s (rotating, 10MB x 5 backups)",
        log_path,
        extra={"step": "0"},
    )

    # VCR-0004 / Phase 2 — the registry is the single source of truth for
    # both the module (the registry key) and the class
    # (``registration.class_name``). An unregistered ``--strategy`` cannot
    # launch: the registry is also the dropdown's contract, so any
    # importable-but-unregistered module (``buy_and_hold``,
    # ``spy_vwap_reversion``) is intentionally non-deployable.
    try:
        from app.routers.engine import _STRATEGY_REGISTRY
    except Exception as exc:  # pragma: no cover - registry import crash is a deploy bug
        print(f"[START] could not load strategy registry: {exc}", file=sys.stderr)
        return 2
    registration = _STRATEGY_REGISTRY.get(args.strategy)
    if registration is None:
        print(
            f"[START] strategy {args.strategy!r} is not registered. Registered "
            f"strategies: {sorted(_STRATEGY_REGISTRY)}.",
            file=sys.stderr,
        )
        return 2
    try:
        module = import_module(f"app.engine.strategy.algorithms.{args.strategy}")
    except ImportError as exc:
        print(f"[START] could not import strategy module {args.strategy!r}: {exc}", file=sys.stderr)
        return 2
    strategy_cls = getattr(module, registration.class_name, None)
    if strategy_cls is None:
        print(
            f"[START] strategy module {args.strategy!r} has no class "
            f"{registration.class_name!r} (registered in StrategyRegistration.class_name).",
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

    # Test injection points: ``args.broker`` (set programmatically) lets
    # tests pass a FakeBroker without an IBKR client; ``args.client``
    # lets tests pass a stub client that records connect/disconnect
    # calls so assertions about lifecycle paths (disconnect-on-position-
    # check-failure, etc.) are testable. Operator runs supply neither.
    #
    # Both paths construct the broker explicitly so cmd_start holds the
    # same instance the engine will use. The recovery-flatten path
    # (``_resolve_recovery_broker``) reads ``broker`` directly — fresh
    # adapter construction would orphan ``_owned_order_ids`` and skip
    # cancelling the runner's actual in-flight orders.

    # Indicator-state persistence policy. ``getattr`` default of None
    # means "no hydration" for test Namespace objects that don't set
    # this attribute (e.g. shutdown-path tests that build argparse.Namespace
    # directly). CLI-constructed args always have the parser default ("require").
    _raw_policy = getattr(args, "hydrate_policy", None)
    from app.engine.live.indicator_state import HydratePolicy, IndicatorStateHydrationError

    if _raw_policy is not None:
        hydrate_policy = HydratePolicy(_raw_policy)
    else:
        hydrate_policy = None

    _artifacts_root = getattr(args, "artifacts_root", Path("PythonDataService/artifacts"))

    # UI-0 identity binding: resolve the strategy_instance_id from the
    # ledger (schema 1.1). It is the durable key for the desired-state
    # sidecar, the live-state sidecar, and LiveEngine. A legacy/empty
    # ledger (schema 1.0, or 1.1 with no binding) falls back to
    # --strategy so older runs still operate, but the operator is warned
    # because there is then no O(1) run_id -> strategy_instance_id mapping
    # for the UI to key the durable controls off. --strategy stays the
    # algorithm MODULE / strategy_key used for dynamic import below.
    strategy_instance_id = ledger.strategy_instance_id or args.strategy
    if not ledger.strategy_instance_id:
        logger.warning(
            "no strategy_instance_id binding in ledger (schema_version=%s); "
            "falling back to --strategy=%r as the instance id. The durable "
            "desired-state and live-state sidecars will be keyed off this "
            "fallback, not a ledger-persisted identity.",
            ledger.schema_version,
            args.strategy,
        )

    # Durable operator desired-state gate (PRD-A § 16.4 Resolution 7 /
    # PR-D). Keyed by the resolved strategy_instance_id, it outlives any
    # single run_id: a bot PAUSED before a crash resumes paused; a
    # STOPPED bot refuses to restart on its own. A corrupt control file is
    # a refusal, never a clean restart — same invariant as poisoned.flag
    # above.
    from app.engine.live.desired_state import (
        DesiredState,
        DesiredStateCorruptError,
        DesiredStateRepo,
        stable_desired_state_path,
    )

    desired_state_path = stable_desired_state_path(_artifacts_root, strategy_instance_id)
    desired_repo = DesiredStateRepo(desired_state_path)
    try:
        desired = desired_repo.read_state()
    except DesiredStateCorruptError as exc:
        print(
            f"[START] desired_state.json at {desired_state_path} is corrupted: {exc}",
            file=sys.stderr,
        )
        return 1
    if desired is DesiredState.STOPPED:
        print(
            f"[START] HALT — desired_state=STOPPED for {strategy_instance_id} "
            f"({desired_state_path}). § 16.4 Resolution 7: clear it with "
            f"'run.py resume' before the bot will start.",
            file=sys.stderr,
        )
        return 1
    start_paused = desired is DesiredState.PAUSED
    if start_paused:
        logger.info(
            "Booting paused: durable desired_state=PAUSED for %s",
            strategy_instance_id,
            extra={"step": "0"},
        )

    def _write_desired_state(state: DesiredState, reason: str) -> None:
        desired_repo.set(state, updated_by="engine", reason=reason, now_ms=now_ms())

    # Resolve the decision-row schema + provenance from the strategy spec
    # the ledger pins (PRD-A §16.1 Resolution 5). Loaded BEFORE the client
    # so spec.client_id can pin the Gateway clientId (§16.3 isolation
    # invariant). The spec is authoritative for the strategy-specific
    # columns, submit_mode (the run's mode), bar_source, and client_id.
    # If the spec can't be loaded at runtime (path moved since init-ledger,
    # parse error), fall back to the default EMA schema + env client id so
    # a decisions.parquet is still produced — and log it.
    decision_columns = DECISION_COLUMNS
    run_mode = "live_paper"
    bar_source = "ibkr_paper_delayed"
    spec_client_id: int | None = None
    try:
        spec = load_spec_from_path(Path(ledger.strategy_spec_path))
        decision_columns = resolve_decision_columns(spec)
        run_mode = spec.submit_mode
        bar_source = spec.bar_source_descriptor
        spec_client_id = spec.client_id
    except (OSError, ValueError) as exc:
        logger.warning(
            "could not load strategy spec at %s for decision schema; falling back to default EMA schema: %s",
            ledger.strategy_spec_path,
            exc,
        )

    broker = getattr(args, "broker", None)
    client = getattr(args, "client", None)
    if broker is None:
        if client is None:
            client = _make_ibkr_client(spec_client_id)
        from app.engine.live.live_portfolio import IbkrBrokerAdapter

        broker = IbkrBrokerAdapter(client)
    # Live-state sidecar writer (PRD-A § 16.4 Resolution 3 / PR-E).
    # Only wired when an IBKR client is present — replay tests pass
    # broker= directly with client=None and don't need the order-
    # idempotency sidecar. Failures inside the writer are swallowed by
    # LiveEngine._persist_live_state so a sidecar I/O hiccup doesn't
    # crash the bar loop.
    live_state_writer = _build_live_state_writer(
        strategy_instance_id=strategy_instance_id,
        run_id=ledger.run_id,
        client=client,
        artifacts_root=_artifacts_root,
    )
    live_state_seed = _build_live_state_seed_envelope(
        strategy_instance_id=strategy_instance_id,
        run_id=ledger.run_id,
        client=client,
        last_processed_bar_ms=ledger.start_date_ms,
    )
    owned_perm_ids: set[int] = set()
    if live_state_writer is not None:
        from app.engine.live.live_state_sidecar import stable_live_state_path

        owned_perm_ids = _read_owned_perm_ids(stable_live_state_path(_artifacts_root, strategy_instance_id))

    # Operator command channel (PRD-A § 16.4 Resolution 7 / PR-D). The
    # bot polls ``<run_dir>/commands/`` at 1s, independent of the bar
    # loop, for PAUSE / RESUME / STOP / FLATTEN / RECONCILE /
    # MARK_POISONED. Always wired on a CLI start (the dir is created
    # lazily by the channel); replay tests construct LiveEngine
    # directly and leave command_channel=None.
    from app.engine.live.command_channel import CommandChannel

    command_channel = CommandChannel(args.run_dir / "commands")

    # session_start_ms is the WALL-CLOCK moment this broker session began —
    # the floor for the outside-mutation check (halt.check_outside_mutation).
    # Distinct from ledger.start_date_ms, which is the trading-date anchor
    # hashed into the run_id; that value comes from the deploy request and
    # can be any moment of "today" (e.g. midnight UTC for a date-aligned
    # convention). Wiring the trading-date anchor here would let a fill that
    # happened *minutes before this run started* slip past the floor whenever
    # the operator anchored start_date_ms to midnight — exactly the bug the
    # 2026-06-12 smoke run hit (foreign sell at 18:20 UTC was treated as
    # "after session start 00:00 UTC" and tripped a false outside_mutation
    # halt).
    session_start_ms = int(time.time() * 1000)
    # PRD #619-A §A3 — production wiring of the broker-safety verdict
    # observer. The closure reads the *child's own* IbkrClient via the
    # typed ``BrokerRuntimeSnapshot`` builder and emits the ADR-0011
    # identity verdict that ``LiveEngine._check_verdict_transition_halt``
    # writes to ``verdict_snapshot.json`` every check. The data-plane
    # singleton is never consulted for live-run safety authority. Tests
    # that pass ``broker=`` directly without a client (no real IBKR
    # session) leave the provider unset — the engine treats ``None`` as
    # "no observer wired" and skips the gate.
    verdict_provider = (
        make_live_engine_verdict_provider(client) if client is not None else None
    )

    # PRD #619-B B3 — engine_runtime.json producer wiring. The
    # aggregator + publisher live for the lifetime of one engine.run().
    # The publisher is started just before engine.run() and stopped in
    # the outer finally so a transient writer failure cannot leave a
    # stale snapshot on disk. ``expected_daemon_boot_id`` comes from
    # the env var the daemon sets when spawning the child (619-B
    # daemon-integration follow-up); ``None`` until that integration
    # lands. ``process_start_identity`` is a fresh per-process id so
    # the orphan classifier (619-B B6) can distinguish two runs of the
    # same instance.
    from uuid import uuid4

    from app.engine.live.engine_runtime_publisher import (
        EngineRuntimeAggregator,
        EngineRuntimePublisher,
    )

    _runtime_aggregator = EngineRuntimeAggregator(
        strategy_instance_id=strategy_instance_id,
        run_id=ledger.run_id,
        pid=_os.getpid(),
        process_start_identity=uuid4().hex,
        expected_daemon_boot_id=_os.environ.get("LIVE_RUNNER_DAEMON_BOOT_ID"),
    )
    _runtime_publisher = EngineRuntimePublisher(
        _runtime_aggregator,
        run_dir=args.run_dir,
        now_ms=lambda: int(time.time() * 1000),
    )
    engine = LiveEngine(
        client,
        live_config,
        broker=broker,
        output_dir=args.run_dir,
        account_id=ledger.account_id,
        readonly=args.readonly,
        max_orders_per_day=args.max_orders_per_day,
        artifacts_root=_artifacts_root,
        hydrate_policy=hydrate_policy,
        session_start_ms=session_start_ms,
        code_sha=ledger.code_sha,
        strategy_spec_sha=ledger.strategy_spec_sha256,
        live_state_writer=live_state_writer,
        command_channel=command_channel,
        start_paused=start_paused,
        desired_state_writer=_write_desired_state,
        run_id=ledger.run_id,
        strategy_key=args.strategy,
        strategy_instance_id=strategy_instance_id,
        run_mode=run_mode,
        bar_source=bar_source,
        decision_columns=decision_columns,
        owned_perm_ids=owned_perm_ids,
        sizing_surface=_lookup_sizing_surface(args.strategy),
        # Phase 5A wiring (VCR-0002 hot-fix) — give the engine the path
        # to the run's intent_events.jsonl so it can construct an
        # IntentWal on LivePortfolio. Required since Phase 5B (ADR 0008)
        # fail-fasts when a real-broker portfolio has no IntentWal.
        intent_wal_path=args.run_dir / "intent_events.jsonl",
        verdict_provider=verdict_provider,
        runtime_aggregator=_runtime_aggregator,
        artifacts_root_for_lease=_artifacts_root,
        watchdog_factory=_build_child_watchdog_factory(_artifacts_root, args.run_dir),
    )

    # PRD #619-A — capture the durable child/run evidence the Resume
    # gate needs to evaluate ``submission_capability``: the declared
    # submit mode from the spec (``run_mode`` was resolved above from
    # ``spec.submit_mode``) and the actual ``readonly`` setting used to
    # construct the child. Both are immutable startup facts; subsequent
    # ``write_run_status`` calls use ``model_copy(update={...})`` so
    # they carry through every lifecycle write. A run whose status
    # sidecar predates this PR will have ``None`` here and the Resume
    # gate honestly reports capability=UNKNOWN.
    _submit_mode_at_start: Literal["live_paper", "shadow"] | None = (
        run_mode if run_mode in ("live_paper", "shadow") else None
    )
    _entry_sidecar = RunStatusSidecar(
        run_id=ledger.run_id,
        started_at_ms=now_ms(),
        last_update_ms=now_ms(),
        host_pid=_os.getpid(),
        submit_mode_at_start=_submit_mode_at_start,
        readonly_at_start=bool(args.readonly),
    )
    try:
        write_run_status(args.run_dir, _entry_sidecar)
    except OSError as exc:
        logger.warning(
            "Could not write entry sidecar for run %s in %s: %s",
            ledger.run_id,
            args.run_dir,
            exc,
        )

    bars_iter = getattr(args, "bars", None)
    print(
        f"[START] run_id={ledger.run_id} account={ledger.account_id} "
        f"readonly={args.readonly} max_orders_per_day={args.max_orders_per_day}"
    )

    async def _drive_engine() -> int:
        loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()
        _install_signal_handlers(loop, shutdown_event)

        # Production path: connect the IBKR client before validating
        # the paper sentinel inside engine.run. Until this commit the
        # CLI created IbkrClient() but never called connect(), so the
        # engine's _validate_paper_client would raise "requires a DU
        # paper account, got None". The injected-broker test path
        # skips this — FakeBroker is always "connected."
        if client is not None:
            try:
                await client.connect()
            except Exception as exc:
                # connect() runs BEFORE the outer try/finally below, so a
                # connect failure (clientId collision / IbkrClientIdInUseError,
                # paper-sentinel refusal, port/broker error) would otherwise
                # propagate uncaught through asyncio.run() and crash the process
                # with no terminal status sidecar — the entry sidecar keeps
                # exit_code=None, so the console "Why It Stopped" panel goes
                # blank and the instance looks stuck "starting". Record the exit
                # (3 = runtime broker/IO error, mirroring the position-fetch
                # failure path below) so the operator sees the real reason.
                logger.exception("IBKR connect() failed before session start", extra={"step": "1"})
                print(
                    f"[START] could not connect to IBKR: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                write_run_status(
                    args.run_dir,
                    _entry_sidecar.model_copy(
                        update={
                            "ended_at_ms": now_ms(),
                            "last_update_ms": now_ms(),
                            "exit_code": 3,
                            "exit_reason": ExitReason.exception,
                        }
                    ),
                )
                return 3
        # Outer try/finally guarantees ``client.disconnect()`` runs on
        # EVERY post-connect exit path — including the early returns
        # from the unexpected-position gate (return 2 on fetch failure,
        # return 1 on bad position). Without it, a position-gate halt
        # would leak the IBKR session and interfere with the next
        # operator run. (Reviewer feedback on PR #233.)
        try:
            # Unexpected-position gate (spec § 5 single-client invariant
            # + § 7 broker-state-divergence). The pre-flight subcommand
            # also provides this check, but only when the operator
            # passes ``--positions-json``; the "live runner enforces
            # this when connected to IB" message that pre-flight prints
            # in the no-flag path was previously vacuous because no
            # such enforcement existed. Now it does: cmd_start fetches
            # broker positions after connect and refuses to start if
            # the account holds anything beyond a long position in the
            # strategy's expected symbol. Runs against any broker
            # (production IbkrBrokerAdapter or test FakeBroker) since
            # both expose ``fetch_positions``.
            try:
                positions = await broker.fetch_positions()
            except Exception as exc:
                print(
                    f"[START] could not fetch positions for unexpected-position check: {exc}",
                    file=sys.stderr,
                )
                # Exit 3 per the module docstring exit-code table — a
                # broker fetch failure is a runtime error (broker / IO),
                # not an operator-error condition. Exit 2 would imply
                # bad args or missing files. Record a terminal status so the
                # console shows the reason instead of a blank "stuck starting".
                write_run_status(
                    args.run_dir,
                    _entry_sidecar.model_copy(
                        update={
                            "ended_at_ms": now_ms(),
                            "last_update_ms": now_ms(),
                            "exit_code": 3,
                            "exit_reason": ExitReason.exception,
                        }
                    ),
                )
                return 3
            # The host daemon injects sibling instances' symbols via
            # --managed-symbols (ADR 0005, completes #395) so a sibling's
            # position is not misread as foreign contamination. Absent (a run
            # started in isolation) -> defaults to {symbol}.
            managed_symbols = (
                {s.strip() for s in args.managed_symbols.split(",") if s.strip()}
                if getattr(args, "managed_symbols", None)
                else None
            )
            position_check = check_unexpected_position(
                positions, expected_symbol=live_config.symbol, managed_symbols=managed_symbols
            )
            # ADR 0009 § 9 / Decision 13 — symbol-scoped all-in coexistence
            # guard. The sibling symbol list is the same one --managed-symbols
            # ships; we restrict it here to siblings that are themselves
            # SetHoldings(1.0) live — the daemon passes that subset via
            # --sibling-all-in-symbols. Absent ⇒ no siblings (a single-instance
            # run). The check is a hard halt on its own; positions are already
            # in flight on the same broker probe, so we don't re-fetch.
            sibling_all_in = (
                {s.strip() for s in args.sibling_all_in_symbols.split(",") if s.strip()}
                if getattr(args, "sibling_all_in_symbols", None)
                else None
            )
            coexistence_check = check_all_in_coexistence(
                proposed_symbol=live_config.symbol,
                proposed_sizing=live_config.sizing,
                broker_positions=positions,
                sibling_all_in_symbols=sibling_all_in,
            )
            if not coexistence_check.passed:
                print(
                    f"[START] HALT all_in_coexistence: {coexistence_check.detail}",
                    file=sys.stderr,
                )
                write_run_status(
                    args.run_dir,
                    _entry_sidecar.model_copy(
                        update={
                            "ended_at_ms": now_ms(),
                            "last_update_ms": now_ms(),
                            "exit_code": 1,
                            "exit_reason": ExitReason.fatal_halt,
                        }
                    ),
                )
                return 1
            if not position_check.passed:
                print(
                    f"[START] HALT unexpected_position: {position_check.detail} "
                    f"(expected long-only {live_config.symbol}; operator must "
                    f"reconcile the account before starting)",
                    file=sys.stderr,
                )
                # A contaminated-account refusal is a halt (exit 1); record a
                # terminal status so "Why It Stopped" explains it rather than the
                # instance looking stuck "starting".
                write_run_status(
                    args.run_dir,
                    _entry_sidecar.model_copy(
                        update={
                            "ended_at_ms": now_ms(),
                            "last_update_ms": now_ms(),
                            "exit_code": 1,
                            "exit_reason": ExitReason.fatal_halt,
                        }
                    ),
                )
                return 1

            # ADR-0008 §5 / cold-start reconciliation gate (PR 1). Wired
            # AFTER the unusual-position check and BEFORE engine.run() so
            # the engine never enters its bar loop without a durable
            # ``reconciliation_receipt.json``. Only runs when an IBKR
            # client is present (production path); replay / FakeBroker
            # tests pass client=None and the engine has no submit side
            # effects to reconcile.
            if client is not None and live_state_writer is not None:
                from app.broker.ibkr.client import BrokerError
                from app.broker.ibkr.orders import (
                    executions_for_reconnect_recovery,
                    list_open_orders,
                )
                from app.engine.live.live_state_sidecar import (
                    LiveStateSidecarRepo,
                    stable_live_state_path,
                )
                from app.engine.live.reconciliation_classifier import (
                    Adopt,
                    Poison,
                )
                from app.engine.live.reconciliation_orchestrator import reconcile

                _live_state_path = stable_live_state_path(_artifacts_root, strategy_instance_id)
                _sidecar_repo = LiveStateSidecarRepo(_live_state_path)
                _bot_order_namespace = (
                    live_state_seed.bot_order_namespace
                    if live_state_seed is not None
                    else f"learn-ai/{strategy_instance_id}/v1"
                )

                # Step 2 (per spec): synchronize broker caches before
                # building the snapshot. An empty cache cannot satisfy
                # reconciliation (Acceptance Gate #2) — these calls
                # populate ib_async's open-orders + executions caches.
                try:
                    _open_orders = await list_open_orders(client)
                    _executions = await executions_for_reconnect_recovery(client)
                except BrokerError as exc:
                    logger.exception(
                        "IBKR broker cache sync failed before reconciliation",
                        extra={"step": "reconcile-sync"},
                    )
                    print(
                        f"[START] could not sync broker caches for reconciliation: {exc}",
                        file=sys.stderr,
                    )
                    write_run_status(
                        args.run_dir,
                        _entry_sidecar.model_copy(
                            update={
                                "ended_at_ms": now_ms(),
                                "last_update_ms": now_ms(),
                                "exit_code": 3,
                                "exit_reason": ExitReason.exception,
                            }
                        ),
                    )
                    return 3

                _broker_snapshot = _build_broker_snapshot_from_ibkr(
                    _open_orders, _executions
                )

                _prior_run_dir = _resolve_prior_run_dir(
                    current_run_dir=args.run_dir,
                    strategy_instance_id=strategy_instance_id,
                    current_created_ms=ledger.created_at_ms,
                )

                async def _probe():
                    return _broker_snapshot

                try:
                    _reconcile_result = await reconcile(
                        run_dir=args.run_dir,
                        sidecar=_sidecar_repo,
                        broker_probe=_probe,
                        allowed_namespaces=frozenset({_bot_order_namespace}),
                        now_ms=now_ms,
                        prior_run_dir=_prior_run_dir,
                        # The per-instance sidecar may still carry the prior
                        # run's identity until the new engine flushes — pass
                        # the *new* run's ledger identity so the receipt is
                        # stamped against the run we're actually starting.
                        current_run_id=ledger.run_id,
                        current_strategy_instance_id=strategy_instance_id,
                        current_namespace=_bot_order_namespace,
                    )
                except Exception as exc:
                    # Receipt-write failure is fatal per the contract
                    # "no submit without a durable receipt." Surface it as
                    # exit 3 (runtime broker/IO) and refuse to enter
                    # engine.run.
                    logger.exception(
                        "Reconciliation receipt write failed; refusing startup",
                        extra={"step": "reconcile"},
                    )
                    print(
                        f"[START] reconciliation receipt write failed: {exc}",
                        file=sys.stderr,
                    )
                    write_run_status(
                        args.run_dir,
                        _entry_sidecar.model_copy(
                            update={
                                "ended_at_ms": now_ms(),
                                "last_update_ms": now_ms(),
                                "exit_code": 3,
                                "exit_reason": ExitReason.exception,
                            }
                        ),
                    )
                    return 3

                _verdict = _reconcile_result.verdict
                if isinstance(_verdict, Poison):
                    # poisoned.flag has already been stamped by the
                    # orchestrator; refuse startup with the fatal-halt exit.
                    print(
                        f"[START] HALT cold_start_divergence: {_verdict.reason}",
                        file=sys.stderr,
                    )
                    write_run_status(
                        args.run_dir,
                        _entry_sidecar.model_copy(
                            update={
                                "ended_at_ms": now_ms(),
                                "last_update_ms": now_ms(),
                                "exit_code": 1,
                                "exit_reason": ExitReason.fatal_halt,
                            }
                        ),
                    )
                    return 1
                if isinstance(_verdict, Adopt) and _verdict.pause:
                    # Ambiguous exposure: an adopted order is still active
                    # at the broker. Persist durable PAUSED so a restart
                    # remains paused, AND flip the engine's in-memory
                    # ``_paused`` flag — the engine seeds its constructor
                    # ``start_paused`` from the pre-reconcile desired_state
                    # snapshot and does not re-read the file before bar 1.
                    # Without the in-memory flip an adopted active order
                    # could be followed by a new submission on the next bar
                    # instead of being held for operator resolution.
                    _write_desired_state("PAUSED", reason="reconcile_ambiguous_exposure")
                    engine._paused = True

            try:
                # PRD #619-B B3 — start the runtime publisher just
                # before the bar loop so the first publish lands as
                # soon as the engine's startup hooks populate all
                # four blocks. ``finally`` below stops it bounded so
                # a final snapshot is flushed.
                await _runtime_publisher.start()
                await engine.run(strategy, bars=bars_iter, shutdown_event=shutdown_event)
                # Write exit sidecar — use keyboard_interrupt if signal was received
                if shutdown_event.is_set():
                    _exit_reason = ExitReason.keyboard_interrupt
                else:
                    _exit_reason = ExitReason.normal
                write_run_status(
                    args.run_dir,
                    _entry_sidecar.model_copy(
                        update={
                            "ended_at_ms": now_ms(),
                            "last_update_ms": now_ms(),
                            "exit_code": 0,
                            "exit_reason": _exit_reason,
                        }
                    ),
                )
                return 0
            except IndicatorStateHydrationError as exc:
                # Exit 4: REQUIRE policy, sidecar absent or invalid. Distinct
                # from 1 (halt) / 2 (operator error) / 3 (runtime IO error).
                # Must be caught before the generic Exception handler below
                # so 3-from-unhandled doesn't preempt the more specific code.
                logger.error(
                    "indicator-state hydrate failed (%s); see %s",
                    exc.receipt.validation.failure_reason,
                    args.run_dir / "indicator_state_hydration.json",
                )
                write_run_status(
                    args.run_dir,
                    _entry_sidecar.model_copy(
                        update={
                            "ended_at_ms": now_ms(),
                            "last_update_ms": now_ms(),
                            "exit_code": 4,
                            "exit_reason": ExitReason.exception,
                        }
                    ),
                )
                return 4
            except FatalHaltError as exc:
                print(
                    f"[START] FATAL HALT — {exc.reason.trigger.value} at "
                    f"{exc.reason.halted_at_ms}ms UTC; details={exc.reason.details}",
                    file=sys.stderr,
                )
                write_run_status(
                    args.run_dir,
                    _entry_sidecar.model_copy(
                        update={
                            "ended_at_ms": now_ms(),
                            "last_update_ms": now_ms(),
                            "exit_code": 1,
                            "exit_reason": ExitReason.fatal_halt,
                        }
                    ),
                )
                return 1
            except MaxOrdersPerDayExceeded as exc:
                print(f"[START] HALT — max orders per day exceeded: {exc}", file=sys.stderr)
                write_run_status(
                    args.run_dir,
                    _entry_sidecar.model_copy(
                        update={
                            "ended_at_ms": now_ms(),
                            "last_update_ms": now_ms(),
                            "exit_code": 1,
                            "exit_reason": ExitReason.max_orders_exceeded,
                        }
                    ),
                )
                return 1
            except _AccountIdentityError as exc:
                # VCR-0006 / Phase 3 — account identity refusal at the start
                # gate. NO recovery flatten — touching orders on the wrong
                # account is exactly what the gate is preventing. Exit 1 with
                # both raw values surfaced for the cockpit failure list.
                print(f"[START] HALT — broker account identity refusal: {exc}", file=sys.stderr)
                write_run_status(
                    args.run_dir,
                    _entry_sidecar.model_copy(
                        update={
                            "ended_at_ms": now_ms(),
                            "last_update_ms": now_ms(),
                            "exit_code": 1,
                            "exit_reason": ExitReason.fatal_halt,
                        }
                    ),
                )
                return 1
            except Exception as exc:
                logger.exception(
                    "Unhandled exception in engine.run — attempting recovery flatten",
                    extra={"step": "8"},
                )
                print(f"[START] runtime error: {type(exc).__name__}: {exc}", file=sys.stderr)
                write_run_status(
                    args.run_dir,
                    _entry_sidecar.model_copy(
                        update={
                            "ended_at_ms": now_ms(),
                            "last_update_ms": now_ms(),
                            "exit_code": 3,
                            "exit_reason": ExitReason.exception,
                        }
                    ),
                )
                broker_for_flatten = _resolve_recovery_broker(broker, client)
                if broker_for_flatten is not None:
                    is_readonly = _is_recovery_readonly(args, client)
                    try:
                        live_state_path = None
                        if live_state_writer is not None:
                            from app.engine.live.live_state_sidecar import stable_live_state_path

                            live_state_path = stable_live_state_path(_artifacts_root, strategy_instance_id)
                        n = await _recovery_flatten(
                            broker_for_flatten,
                            readonly=is_readonly,
                            live_state_path=live_state_path,
                            live_state_seed=live_state_seed,
                            bot_order_namespace=f"learn-ai/{strategy_instance_id}/v1",
                        )
                        if is_readonly:
                            print(
                                f"[START] readonly: recovery flatten skipped — "
                                f"{n} position(s) detected; run "
                                f"'emergency-flatten --confirm' if cleanup needed",
                                file=sys.stderr,
                            )
                        else:
                            print(
                                f"[START] recovery flatten submitted {n} order(s); "
                                f"verify with /api/broker/positions and /api/broker/orders/open",
                                file=sys.stderr,
                            )
                    except Exception:
                        logger.exception(
                            "Recovery flatten itself failed",
                            extra={"step": "8"},
                        )
                        print(
                            "[START] recovery flatten failed — manual cleanup via 'emergency-flatten --confirm' required",
                            file=sys.stderr,
                        )
                else:
                    print(
                        "[START] no live broker for recovery flatten — operator should run "
                        "'emergency-flatten --confirm' to verify and clean up positions",
                        file=sys.stderr,
                    )
                return 3
        finally:
            # PRD #619-B B3 — bounded shutdown of the runtime publisher
            # BEFORE the client disconnect so a final snapshot
            # captures the disconnect-imminent state, not a half-torn
            # post-disconnect state.
            try:
                await _runtime_publisher.stop()
            except Exception:
                logger.exception("runtime publisher stop failed", extra={"step": "8"})
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    logger.exception("client.disconnect() failed", extra={"step": "8"})

    rc = asyncio.run(_drive_engine())
    if rc == 0:
        print("[START] run completed cleanly")
    return rc


# ──────────────────────────── emergency-flatten subcommand ───────────


def cmd_emergency_flatten(args: argparse.Namespace) -> int:
    """Manually liquidate every position on a contaminated account (§ 7.2 #6).

    The ONLY allowed action on an account whose run is poisoned. Per
    spec § 7.2 #6: "places only liquidating orders, logs each one,
    and writes to a separate live_runs/<run_id>/emergency_flatten.log.
    This path is OFF by default and never auto-triggered."

    Defense-in-depth:
      * ``--confirm`` is required (typo-proofing — the operator must
        explicitly opt in).
      * ``--account DU...`` must match the IBKR-connected account
        (typo-proofing — the operator must name the account they
        intend to flatten, and we refuse if it doesn't match).
      * Refuses on a non-paper account by virtue of the IbkrClient
        connect-time DU sentinel (§ 5).

    The subcommand is independent of LiveEngine — it makes its own
    broker calls so a poisoned LiveEngine state has no influence on
    the flatten path. Unlike ``start``, it does NOT read the run
    ledger; the operator names the account directly so a corrupted
    ledger doesn't block the flatten.
    """
    import asyncio as _asyncio
    from datetime import UTC
    from datetime import datetime as _datetime

    from app.broker.ibkr.models import IbkrOrderSpec
    from app.engine.live.order_identity import (
        build_bot_order_namespace,
        build_order_ref,
        mint_intent_id,
    )

    if not args.confirm:
        print(
            "[EMERGENCY-FLATTEN] refusing without --confirm. This subcommand places "
            "broker orders against a real (paper) account; the operator must opt in.",
            file=sys.stderr,
        )
        return 2

    # VCR-0020 — emergency-flatten runs outside any engine session, so no
    # ``bot_order_namespace`` exists for it to inherit. Synthesise one from
    # the account id so every spec carries a deterministic ``order_ref`` and
    # the Phase 5A ``place_paper_order`` invariant is satisfied. ``eflat-``
    # short prefix keeps the 60-char ``order_ref`` cap intact even for
    # 8-char DU* accounts. Using ``build_bot_order_namespace`` keeps the
    # ``learn-ai/{sid}/v1`` shape downstream parsers expect.
    emergency_namespace = build_bot_order_namespace(f"eflat-{args.account}")

    log_path = args.run_dir / "emergency_flatten.log"
    args.run_dir.mkdir(parents=True, exist_ok=True)

    def _log(message: str) -> None:
        line = f"{_datetime.now(UTC).isoformat()} {message}"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(f"[EMERGENCY-FLATTEN] {message}")

    _log(f"start: account={args.account} run_dir={args.run_dir}")

    # Test injection points: ``args.broker`` and ``args.client``. When
    # the broker is supplied, no client is needed (FakeBroker is always
    # "connected"). The production path constructs both so the IBKR
    # connection lifecycle is owned end-to-end.
    broker = getattr(args, "broker", None)
    client = getattr(args, "client", None)
    if broker is None:
        from app.broker.ibkr.client import IbkrClient
        from app.engine.live.live_portfolio import IbkrBrokerAdapter

        if client is None:
            client = IbkrClient()
        broker = IbkrBrokerAdapter(client)

    async def _flatten() -> int:
        # Connect the IBKR client before any broker call. ``fetch_positions``
        # → ``account.fetch_account_summary`` calls ``client.require_connected()``
        # which raises if the client never connected, so without this
        # ``emergency-flatten --confirm`` would fail at the very first
        # broker call in production. Disconnect runs in ``finally`` to
        # match cmd_start's lifecycle.
        if client is not None:
            await client.connect()
        try:
            snapshot = await broker.fetch_positions()
            if snapshot.account_id.upper() != args.account.upper():
                _log(f"REFUSED: connected account {snapshot.account_id} != --account {args.account}; no orders placed.")
                return 2
            # VCR-0009 — cancel owned open orders BEFORE liquidating. The
            # sibling paths (``_recovery_flatten`` and
            # ``LiveEngine._flatten``) already do this; ``cmd_emergency_flatten``
            # was the asymmetric outlier and could race an open SELL limit
            # against the emergency SELL market, double-selling on a panic.
            # ``cancel_open_orders`` is namespace-scoped: it only touches the
            # runner's own orders, never an unrelated open order on the
            # same paper account. Full Phase 5C ownership-query gating
            # (OWNERSHIP_QUERY_UNAVAILABLE_HALT, cancel-confirm timeout
            # with EMERGENCY_FLATTEN_WITH_UNCONFIRMED_CANCELS carve-out)
            # is the deferred follow-up; this fix closes the immediate
            # cancel-first asymmetry called out in VCR-0009.
            # Phase 5C / VCR-0002 — emergency-flatten force carve-out.
            # The managed paths (LiveEngine._flatten, _recovery_flatten)
            # refuse to liquidate on cancel-confirm timeout. Emergency
            # flatten is the operator-confirmed "panic" surface and DOES
            # proceed past timeout — but the unconfirmed-cancel decision
            # is recorded as an audit event so post-mortems can identify
            # runs that liquidated against possibly-still-open orders.
            from app.engine.live.live_engine import CANCEL_CONFIRM_TIMEOUT_S

            unconfirmed_cancels_reason: str | None = None
            try:
                cancelled = await _asyncio.wait_for(
                    broker.cancel_open_orders(),
                    timeout=CANCEL_CONFIRM_TIMEOUT_S,
                )
                _log(f"cancelled_open_orders: count={len(cancelled)} ids={cancelled}")
            except TimeoutError:
                unconfirmed_cancels_reason = f"cancel_confirm_timeout_s={CANCEL_CONFIRM_TIMEOUT_S}"
                _log(
                    f"WARNING: cancel_open_orders timed out after {CANCEL_CONFIRM_TIMEOUT_S}s; "
                    "proceeding with liquidation anyway (force-flatten)."
                )
            except Exception as exc:
                unconfirmed_cancels_reason = f"{type(exc).__name__}: {exc}"
                # Operator-confirmed force-flatten proceeds even if the cancel
                # call fails — leaving open broker positions during a panic
                # is worse than acting without cancel-confirmation. Logged
                # loudly so the run record shows the unconfirmed cancel.
                _log(
                    f"WARNING: cancel_open_orders failed ({type(exc).__name__}: {exc}); proceeding with liquidation anyway (force-flatten)."
                )

            if unconfirmed_cancels_reason is not None:
                _log(
                    "EMERGENCY_FLATTEN_WITH_UNCONFIRMED_CANCELS reason="
                    f"{unconfirmed_cancels_reason} account={args.account}"
                )

            liquidated = 0
            for pos in snapshot.positions:
                qty_signed = float(pos.quantity)
                if qty_signed == 0:
                    continue
                # Preserve fractional quantities — IbkrOrderSpec.quantity
                # is float and IBKR supports fractional shares for many
                # equities. Casting to int here would truncate (e.g.
                # 0.5 → 0) and submit a zero-sized order that IBKR
                # rejects, leaving the fractional position un-flattened.
                # (CodeRabbit P2 from #193.)
                action = "SELL" if qty_signed > 0 else "BUY"
                # VCR-0020 — mint a fresh intent_id per liquidation, build the
                # full deterministic ``{namespace}:{intent_id}`` token, stamp on
                # the spec. Without this, ``place_paper_order`` refuses the
                # request with OrderRefusedError and the operator's documented
                # panic surface fails on the very first attempt.
                spec = IbkrOrderSpec(
                    symbol=pos.symbol,
                    sec_type=pos.sec_type,
                    action=action,
                    quantity=abs(qty_signed),
                    order_type="MKT",
                    time_in_force="DAY",
                    confirm_paper=True,
                    client_order_id=f"emergency-flatten-{pos.symbol}-{int(_datetime.now(UTC).timestamp() * 1000)}",
                    order_ref=build_order_ref(emergency_namespace, mint_intent_id()),
                )
                ack = await broker.place_order(spec)
                _log(f"liquidated: symbol={pos.symbol} qty={qty_signed} action={action} order_id={ack.order_id}")
                liquidated += 1
            _log(f"complete: liquidated={liquidated}")
            return 0
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    logger.exception("client.disconnect() failed during emergency-flatten")

    try:
        rc = _asyncio.run(_flatten())
    except Exception as exc:
        _log(f"FAILURE: {type(exc).__name__}: {exc}")
        print(f"[EMERGENCY-FLATTEN] runtime error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    return rc


# ──────────────────────────── Argparse ───────────────────────────────


# ──────────────────── desired-state subcommands (PR-D) ───────────────


def _cmd_set_desired_state(args: argparse.Namespace, state) -> int:
    """Shared body for pause/resume/stop: atomically set the durable
    desired-state for a strategy_instance_id (PRD-A § 16.4 Resolution 7).
    """
    from app.engine.live.desired_state import (
        DesiredStateRepo,
        stable_desired_state_path,
    )
    from app.engine.live.run_status import now_ms

    path = stable_desired_state_path(args.artifacts_root, args.strategy_instance_id)
    repo = DesiredStateRepo(path)
    record = repo.set(
        state,
        updated_by=args.updated_by,
        reason=args.reason,
        now_ms=now_ms(),
    )
    print(
        f"[DESIRED-STATE] {args.strategy_instance_id} -> {record.desired_state.value} "
        f"(version {record.version}) at {path}"
    )
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    from app.engine.live.desired_state import DesiredState

    return _cmd_set_desired_state(args, DesiredState.PAUSED)


def _latest_run_dir_for_instance(artifacts_root: Path, strategy_instance_id: str) -> Path | None:
    """Find the most recent ``live_runs/<run_id>/`` whose ``run_ledger.json``
    names this instance. Returns ``None`` if no run dir is found.

    Used by Phase 5D's Resume WAL guard: the operator types ``run.py resume
    --strategy-instance-id <sid>``; we need the WAL of that instance's
    latest run to scan for unresolved uncertain intents. We sort by ledger
    mtime so the most-recent (or in-flight) run wins. A start-time race
    where ``run_ledger.json`` is written by ``init-ledger`` slightly later
    than a pre-existing prior run is acceptable: resume itself is durable
    and idempotent, and the scan is conservative (an unresolved uncertain
    in any of the candidate dirs would be a stronger signal anyway —
    Phase 5D-Follow-up may broaden this to "all runs for this sid").
    """
    import json as _json

    live_runs = artifacts_root / "live_runs"
    if not live_runs.exists():
        return None
    candidates: list[tuple[float, Path]] = []
    for run_dir in live_runs.iterdir():
        if not run_dir.is_dir():
            continue
        ledger_path = run_dir / "run_ledger.json"
        if not ledger_path.exists():
            continue
        try:
            ledger = _json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, _json.JSONDecodeError):
            continue
        if ledger.get("strategy_instance_id") != strategy_instance_id:
            continue
        try:
            mtime = ledger_path.stat().st_mtime
        except OSError:
            continue
        candidates.append((mtime, run_dir))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _scan_verdict_snapshot(snapshot_path: Path) -> str | None:
    """Phase 7B Resume guard #1 (PRD §7B / VCR-0010): return the last
    verdict reading if it is NOT ``"paper-only"``, otherwise ``None``.

    Returns ``None`` when:
      * The snapshot file does not exist (no engine has observed a
        verdict for this instance — older runs predating the snapshot).
      * The snapshot's ``verdict`` field is ``"paper-only"``.
      * The snapshot is unreadable or malformed (fail-open here so a
        spurious file corruption does not jail the operator out of
        Resume entirely; the engine bar loop is still the secondary
        defense and will re-emit a fresh snapshot on the next check).
    """
    if not snapshot_path.exists():
        return None
    try:
        import json as _json

        data = _json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    verdict_value = data.get("verdict") if isinstance(data, dict) else None
    if not isinstance(verdict_value, str):
        return None
    if verdict_value == "paper-only":
        return None
    return verdict_value


def _scan_wal_for_unresolved_uncertains(wal_path: Path) -> list[str]:
    """Phase 5D Resume guard (PRD §5D Resume contract, guard #3): return
    the list of ``intent_id``s whose ``ACK_FAILED_UNCERTAIN`` event has no
    matching resolution event downstream in the same WAL.

    A resolution is any of: ``SUBMITTED_RECOVERED`` (probe PRESENT),
    ``INTENT_NOT_ACCEPTED`` (probe PROVABLY_ABSENT — retry), ``SUBMITTED``
    (the retry succeeded), or ``SUBMIT_UNCERTAIN_HALTED`` (the state machine
    halted on the uncertain — an explicit resolution, not silent drop).

    An empty list means Resume is safe per this guard. A non-empty list
    means the WAL carries an orphaned uncertain ack and the operator must
    reconcile broker state manually before Resume.
    """
    from app.engine.live.intent_events import IntentEventType
    from app.engine.live.intent_wal import IntentWal, IntentWalCorruptError

    if not wal_path.exists():
        return []
    try:
        events = IntentWal(wal_path).read_tail()
    except IntentWalCorruptError:
        # A corrupt WAL must NOT silently allow Resume — surface it as
        # "every intent_id with an uncertain is unresolved" so the caller
        # refuses Resume and the operator inspects the WAL.
        return ["<wal-corrupt-cannot-scan>"]
    pending_uncertain: set[str] = set()
    resolution_types = {
        IntentEventType.SUBMITTED.value,
        IntentEventType.SUBMITTED_RECOVERED.value,
        IntentEventType.INTENT_NOT_ACCEPTED.value,
        IntentEventType.SUBMIT_UNCERTAIN_HALTED.value,
    }
    for event in events:
        et = event.event_type.value
        if et == IntentEventType.ACK_FAILED_UNCERTAIN.value:
            pending_uncertain.add(event.intent_id)
        elif et in resolution_types:
            pending_uncertain.discard(event.intent_id)
    return sorted(pending_uncertain)


def cmd_resume(args: argparse.Namespace) -> int:
    """Set durable ``desired_state=RUNNING`` after the shared Resume
    guards pass (PRD #616).

    The three Resume guards are now resolved through the canonical
    :func:`app.services.resume_guard_state.resolve_guard_state_from_paths`
    so the CLI, the capability projection
    (``operator_surface.actions.resume``), and the desired-state
    mutation endpoint agree on enabled / refused and the reason
    codes.

    The legacy ``--force`` bypass was deleted in PRD #616 — the
    cockpit cannot honestly claim "guarded Resume is structurally
    safe" while a CLI bypass shares the same resolver.  Operators
    that need to clear a guard must resolve the underlying condition
    (re-confirm paper-only broker, reconcile uncertain intents,
    redeploy a poisoned run); each guard has a documented remediation
    path that does not require bypassing the resolver.
    """
    from app.engine.live.desired_state import DesiredState
    from app.services.resume_guard_state import resolve_guard_state_from_paths

    run_dir = _latest_run_dir_for_instance(args.artifacts_root, args.strategy_instance_id)
    if run_dir is not None:
        guard_state = resolve_guard_state_from_paths(
            verdict_snapshot_path=run_dir / "verdict_snapshot.json",
            run_status_path=run_dir / "run_status.json",
            run_dir_for_reconciliation=run_dir,
            intent_wal_path=run_dir / "intent_events.jsonl",
        )
        if not guard_state.allow_resume:
            head = guard_state.reason_codes[0]
            all_codes = ", ".join(guard_state.reason_codes)
            broker_v = guard_state.broker_safety.verdict
            wal_pending = guard_state.uncertain_intent.unresolved_intent_ids
            details: list[str] = []
            if guard_state.broker_safety.state != "SAFE":
                details.append(f"broker-safety={guard_state.broker_safety.state!s} verdict={broker_v!r}")
            # PRD #619-A — submission-capability gate.
            if guard_state.submission_capability.state != "SATISFIED":
                cap = guard_state.submission_capability
                details.append(
                    f"submission-capability={cap.state!s} "
                    f"declared={cap.declared_submit_mode!r} "
                    f"readonly_at_start={cap.readonly_at_start!r}"
                )
            if guard_state.uncertain_intent.state == "PRESENT":
                preview = ", ".join(wal_pending[:5])
                if len(wal_pending) > 5:
                    preview += f", and {len(wal_pending) - 5} more"
                details.append(f"unresolved-uncertain-intent=[{preview}]")
            elif guard_state.uncertain_intent.state == "UNKNOWN":
                details.append("uncertain-intent-state=UNKNOWN (WAL unreadable)")
            if guard_state.reconciliation.state not in {"PASSED", "NOT_AVAILABLE"}:
                details.append(
                    f"reconciliation={guard_state.reconciliation.state!s} ({guard_state.reconciliation.detail or ''})"
                )
            print(
                f"[RESUME] REFUSED ({head}): "
                + "; ".join(details)
                + f"\n  all applicable reason codes: {all_codes}"
                + "\n  resolve the underlying condition; --force was removed in PRD #616.",
                file=sys.stderr,
            )
            return 2

    return _cmd_set_desired_state(args, DesiredState.RUNNING)


def cmd_stop(args: argparse.Namespace) -> int:
    from app.engine.live.desired_state import DesiredState

    return _cmd_set_desired_state(args, DesiredState.STOPPED)


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
    init.add_argument(
        "--strategy-instance-id",
        dest="strategy_instance_id",
        default="",
        help=(
            "Stable identifier for the configured strategy instance (UI-0). "
            "Persisted in run_ledger.json (schema 1.1) and used to key the "
            "durable desired-state sidecar at "
            "artifacts/live_state/<strategy_instance_id>/. NOT part of the "
            "run_id hash. Omit for a legacy/unknown binding (empty); 'start' "
            "then falls back to --strategy with a warning."
        ),
    )
    init.add_argument(
        "--strategy-key",
        dest="strategy_key",
        default="",
        help=(
            "The hand-coded algorithm module this run starts under (the "
            "--strategy arg to 'start'; #416). Persisted in run_ledger.json "
            "(schema 1.2), NOT part of the run_id hash. When set, 'start' "
            "rejects any --strategy that does not match it, closing the "
            "foot-gun where a mismatched algorithm runs against a ledger "
            "reconciled to a different QC backtest. Omit for a legacy/unknown "
            "binding (empty); the guard then no-ops."
        ),
    )
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
    pre.add_argument(
        "--managed-symbols",
        default=None,
        help=(
            "Comma-separated symbols owned by sibling managed strategy instances on this "
            "account. Positions in these symbols are excluded from this instance's "
            "unexpected-position verdict (ADR 0005; fleet contamination is separate). "
            "Defaults to just --expected-symbol."
        ),
    )
    pre.set_defaults(func=cmd_pre_flight)

    # start
    start = sub.add_parser(
        "start",
        help="Run the live engine end-to-end against an existing run directory.",
    )
    start.add_argument(
        "--run-dir", type=Path, required=True, help="live_runs/<run_id>/ directory built by init-ledger."
    )
    start.add_argument(
        "--strategy",
        default="spy_ema_crossover",
        help=(
            "Strategy module / strategy_key under app.engine.strategy.algorithms "
            "(snake_case). Class name is inferred (PascalCase + 'Algorithm'). "
            "This is the algorithm family, NOT the instance id: the durable "
            "strategy_instance_id comes from run_ledger.json (schema 1.1). When "
            "the ledger has no binding (legacy), this value is used as the "
            "instance id fallback."
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
        default=50_000,
        help=(
            "§ 9 cap. Crossing this halts the run with exit 1. Default 50,000 — kept in sync "
            "with the daemon's HostRunnerStartRequest default so a direct CLI launch matches "
            "the control-plane behaviour."
        ),
    )
    start.add_argument(
        "--hydrate-policy",
        choices=["require", "optional", "disabled"],
        default="require",
        help=(
            "Indicator-state hydrate policy. 'require' is the default for the B2 dry-run gate "
            "and paper-week operation; failure to validate the prior session's sidecar exits 4 "
            "before any bar runs. 'optional' is the seed-day mode that cold-starts when no "
            "sidecar exists. 'disabled' skips the read entirely (still writes at end-of-session)."
        ),
    )
    start.add_argument(
        "--allow-cold-start",
        action="store_const",
        const="disabled",
        dest="hydrate_policy",
        help="Alias for --hydrate-policy disabled. The operator escape hatch.",
    )
    start.add_argument(
        "--managed-symbols",
        default=None,
        help=(
            "Comma-separated symbols owned by sibling managed instances on this account "
            "(injected by the host daemon). Positions in these symbols are excluded from "
            "this instance's unexpected-position gate (ADR 0005, completes #395)."
        ),
    )
    start.add_argument(
        "--sibling-all-in-symbols",
        default=None,
        help=(
            "Comma-separated symbols where sibling managed instances on this account "
            "currently hold SetHoldings(1.0) (injected by the host daemon). The "
            "ADR 0009 § 9 / Decision 13 coexistence guard refuses to start a "
            "SetHoldings(1.0) run on any symbol in this set; FixedShares / "
            "FixedNotional starts are never blocked."
        ),
    )
    start.add_argument(
        "--artifacts-root",
        type=Path,
        default=Path("PythonDataService/artifacts"),
        help=(
            "Root directory for cross-session artifacts (indicator state sidecars). "
            "Default: PythonDataService/artifacts (relative to repo root)."
        ),
    )
    start.set_defaults(func=cmd_start)

    # emergency-flatten — § 7.2 #6 manual operator path.
    flatten = sub.add_parser(
        "emergency-flatten",
        help=(
            "Manually liquidate every position on a contaminated account. "
            "Only allowed action on a poisoned run. Requires --confirm + --account."
        ),
    )
    flatten.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="live_runs/<run_id>/ directory; emergency_flatten.log lands under it.",
    )
    flatten.add_argument(
        "--account",
        required=True,
        help="IBKR DU account id; refused if it doesn't match the connected account.",
    )
    flatten.add_argument(
        "--confirm",
        action="store_true",
        help="Required. Without this flag the subcommand refuses (typo-proofing).",
    )
    flatten.set_defaults(func=cmd_emergency_flatten)

    # pause / resume / stop — durable desired-state verbs (PR-D).
    for verb, handler, helptext in (
        ("pause", cmd_pause, "Set durable desired-state PAUSED for a strategy instance."),
        ("resume", cmd_resume, "Set durable desired-state RUNNING (clears PAUSED/STOPPED)."),
        ("stop", cmd_stop, "Set durable desired-state STOPPED; 'start' will then refuse."),
    ):
        verb_parser = sub.add_parser(verb, help=helptext)
        verb_parser.add_argument(
            "--strategy-instance-id",
            required=True,
            dest="strategy_instance_id",
            help="Strategy instance id (the same value passed as 'start --strategy').",
        )
        verb_parser.add_argument(
            "--artifacts-root",
            type=Path,
            default=Path("PythonDataService/artifacts"),
            help="Root for cross-session artifacts; must match 'start --artifacts-root'.",
        )
        verb_parser.add_argument("--reason", default=None, help="Optional operator note recorded in the file.")
        verb_parser.add_argument("--updated-by", default="operator", help="Identity recorded in the file.")
        # PRD #616 — the legacy ``--force`` bypass on `resume` was
        # deleted.  The shared ``ResumeGuardState`` resolver is the
        # authority for every entry point (capability projection,
        # mutation endpoint, CLI); a CLI-only override would
        # invalidate the "guarded Resume is structurally safe" claim.
        verb_parser.set_defaults(func=handler)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
