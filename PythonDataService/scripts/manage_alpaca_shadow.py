"""Operator CLI for the shadow gate (ADR 0059 D2): activate, sessions, receipt.

``activate`` initializes the shadow custody database for one live account and
appends its activation proof -- the one write no startup path performs.
``sessions`` judges every trading day since the shadow instance first ran
against its paper twin. ``receipt`` does the same and, when the gate is
satisfied, seals the result into the receipt store.

Exit codes: ``0`` the command answered; ``1`` the command cannot be run as
asked -- a reserved identity, a binding or database that is not there, a
required count nobody stated, an economic projection the authority will not
vouch for, a receipt the sealer will not accept, or a usage refusal (an absent
flag, an unknown subcommand, a flag outside its bound); ``2`` the named twin
is not this instance's twin, or ``receipt`` found the gate unsatisfied.

Every invocation writes exactly one JSON object to stdout, and every temporal
value in it is ``int64 ms UTC``. (``--help`` is argparse's own usage text on
stdout and exits ``0``; it runs no command.) A usage refusal is therefore a
readable ``error`` key at exit ``1`` rather than argparse's bare exit ``2``,
which used to collide with "the gate is not satisfied".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Mapping
from contextlib import ExitStack, closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.broker.alpaca.broker import ALPACA_LIVE_CAPABILITIES
from app.broker.alpaca.clerk.account_authority import (
    AccountAuthorityIdentityError,
    require_real_account_id,
    shadow_account_id_for_live_account,
)
from app.broker.alpaca.clerk.shadow_authority import activate_shadow_clerk_authority
from app.broker.alpaca.clerk.shadow_receipt import (
    ShadowReceipt,
    ShadowReceiptInvalid,
    ShadowReceiptSession,
    ShadowReceiptStore,
)
from app.broker.alpaca.clerk.shadow_sessions import ShadowSessionLedger
from app.broker.alpaca.clerk.sqlite.economic_projection import EconomicProjectionUnavailable
from app.broker.alpaca.clerk.sqlite.repository import DB_FILENAME
from app.broker.alpaca.clerk.sqlite.writes import confined_account_file
from app.broker.alpaca.config import get_alpaca_settings
from app.broker.ibkr.config import live_artifacts_root
from app.services.alpaca_shadow_reconciliation import (
    EconomicFillSource,
    ShadowGateEvaluation,
    ShadowSessionVerdict,
    ShadowTwinMismatch,
    TwinDayReconciliation,
    evaluate_shadow_gate,
)
from app.services.bot_binding_repository import live_state_binding_repository
from app.utils.timestamps import now_ms_utc
from scripts._operator_cli import timestamp_ms

ShadowGateEvaluator = Callable[..., ShadowGateEvaluation]


class ShadowOperatorRefusal(ValueError):
    """This command cannot be run as asked -- named evidence or a required value is absent."""


def _required_session_count(raw: str) -> int:
    """A gate of zero sessions is satisfied by no evidence, so it is not a gate.

    ``AlpacaSettings.live_shadow_sessions`` is bounded ``ge=1``; this is the
    same bound on the flag that overrides it.
    """
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1 session, not {value}")
    return value


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    # ``exit_on_error=False`` on this parser *and* on every subparser: without
    # it argparse writes usage to stderr and exits 2 on its own, which is the
    # code "the gate is not satisfied" already owns. Each subparser keeps its
    # own flag bounds, so each must refuse the same way.
    parser = argparse.ArgumentParser(
        prog="scripts.manage_alpaca_shadow",
        description="Activate one live account's shadow authority, judge its sessions, seal the receipt.",
        exit_on_error=False,
    )
    parser.add_argument("--live-account-id", required=True)
    parser.add_argument("--artifacts-root", type=Path)
    parser.add_argument("--live-state-root", type=Path)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    subparsers.add_parser(
        "activate",
        help="Initialize this live account's shadow custody database and append its activation proof.",
        exit_on_error=False,
    )
    for operation, help_text in (
        ("sessions", "Report every judged session without writing anything."),
        ("receipt", "Judge the sessions and, when the gate holds, seal the receipt."),
    ):
        gate = subparsers.add_parser(operation, help=help_text, exit_on_error=False)
        gate.add_argument("--strategy-instance-id", required=True)
        gate.add_argument("--twin-account-id", required=True)
        gate.add_argument("--twin-strategy-instance-id", required=True)
        gate.add_argument("--twin-artifacts-root", type=Path)
        gate.add_argument("--required-sessions", type=_required_session_count)
        gate.add_argument("--now-ms", type=timestamp_ms)
    return parser.parse_args(argv)


def _write(payload: Mapping[str, Any]) -> None:
    """One JSON object per invocation, on stdout.

    ``default=str`` is here for the reconciliation's ``Decimal`` prices; every
    temporal value in the payload is already ``int64 ms UTC``.
    """
    sys.stdout.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def _fill_source(db_path: Path, label: str) -> EconomicFillSource:
    """One authority's read-only fill projection, refusing a database that is not there.

    ``sqlite3.connect(..., mode=ro)`` on an absent file raises a bare
    ``OperationalError``; an operator who pointed the CLI at the wrong root
    deserves the path back instead.
    """
    if not db_path.is_file():
        raise ShadowOperatorRefusal(f"{label} custody database not found at {db_path}")
    return EconomicFillSource.from_database_path(db_path)


def _default_evaluate(
    *, shadow_db_path: Path, twin_db_path: Path, **gate_kwargs: Any
) -> ShadowGateEvaluation:
    """Open both authorities' read-only projections, judge the gate, close them.

    The two databases are opened here rather than in :func:`main` so an
    injected evaluator -- the tests' -- touches no database at all.
    """
    with ExitStack() as stack:
        shadow_source = stack.enter_context(closing(_fill_source(shadow_db_path, "shadow")))
        twin_source = stack.enter_context(closing(_fill_source(twin_db_path, "twin")))
        return evaluate_shadow_gate(
            shadow_source=shadow_source, twin_source=twin_source, **gate_kwargs
        )


def _required_sessions(explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    configured = get_alpaca_settings().live_shadow_sessions
    if configured is None:
        raise ShadowOperatorRefusal(
            "--required-sessions is required unless ALPACA_LIVE_SHADOW_SESSIONS is configured"
        )
    return configured


# Everything one day's comparison says about itself except the two order books,
# whose content ``report_sha256`` already names. An allowlist rather than a
# denylist so that a field added to ``TwinDayReconciliation`` is a decision
# here -- the dataclass gained one mid-slice already -- and never a leak.
_REPORTED_RECONCILIATION_FIELDS = (
    "session_open_ms",
    "strategy_instance_id",
    "twin_strategy_instance_id",
    "divergences",
    "max_fill_price_drift",
    "max_fill_time_drift_ms",
    "fill_price_atol",
)


def _reconciliation_payload(reconciliation: TwinDayReconciliation) -> dict[str, Any]:
    """The comparison named by the digest the receipt pins, not by its two order books."""
    payload = asdict(reconciliation)
    reported: dict[str, Any] = {name: payload[name] for name in _REPORTED_RECONCILIATION_FIELDS}
    reported["report_sha256"] = reconciliation.report_sha256()
    return reported


def _session_payload(verdict: ShadowSessionVerdict) -> dict[str, Any]:
    payload = asdict(verdict)
    payload["reconciliation"] = (
        None if verdict.reconciliation is None else _reconciliation_payload(verdict.reconciliation)
    )
    return payload


def _summary(evaluation: ShadowGateEvaluation, *, now_ms: int) -> dict[str, Any]:
    """The judged report, naming the clock it was judged against.

    ``now_ms`` bounds the whole judged day range and is the receipt's
    ``written_at_ms``, so a report that omitted it could not be reproduced
    from its own output.
    """
    return {
        "now_ms": now_ms,
        "live_account_id": evaluation.live_account_id,
        "strategy_instance_id": evaluation.strategy_instance_id,
        "twin_account_id": evaluation.twin_account_id,
        "twin_strategy_instance_id": evaluation.twin_strategy_instance_id,
        "configured_signal_hash": evaluation.configured_signal_hash,
        "satisfied": evaluation.satisfied,
        "counted": len(evaluation.counted),
        "required": evaluation.required_sessions,
        "sessions": [_session_payload(verdict) for verdict in evaluation.sessions],
    }


def _receipt_session(verdict: ShadowSessionVerdict) -> ShadowReceiptSession:
    if verdict.shadow_run_id is None or verdict.reconciliation is None:
        raise RuntimeError(
            f"counted session {verdict.session_open_ms} carries no run id or no reconciliation"
        )
    return ShadowReceiptSession(
        session_open_ms=verdict.session_open_ms,
        shadow_run_id=verdict.shadow_run_id,
        reconciliation_sha256=verdict.reconciliation.report_sha256(),
    )


def _seal(
    evaluation: ShadowGateEvaluation, *, artifacts_root: Path, written_at_ms: int
) -> ShadowReceipt:
    """Seal the counted sessions only, and only for a gate that is satisfied.

    Both of the receipt validator's own invariants follow from that: counted
    verdicts are one per trading day, so no session repeats, and a satisfied
    gate has at least ``required_sessions`` of them.
    """
    receipt = ShadowReceipt.create(
        live_account_id=evaluation.live_account_id,
        strategy_instance_id=evaluation.strategy_instance_id,
        configured_signal_hash=evaluation.configured_signal_hash,
        twin_account_id=evaluation.twin_account_id,
        twin_strategy_instance_id=evaluation.twin_strategy_instance_id,
        required_sessions=evaluation.required_sessions,
        sessions=[_receipt_session(verdict) for verdict in evaluation.counted],
        written_at_ms=written_at_ms,
    )
    ShadowReceiptStore(artifacts_root).append(receipt)
    return receipt


def _activate(*, live_account_id: str, artifacts_root: Path) -> int:
    record = asyncio.run(
        activate_shadow_clerk_authority(
            live_account_id=live_account_id, artifacts_root=artifacts_root
        )
    )
    _write(asdict(record))
    return 0


def _judge(
    args: argparse.Namespace,
    *,
    live_account_id: str,
    artifacts_root: Path,
    evaluate: ShadowGateEvaluator,
) -> int:
    required_sessions = _required_sessions(args.required_sessions)
    now_ms = now_ms_utc() if args.now_ms is None else args.now_ms
    bindings = live_state_binding_repository(args.live_state_root or live_artifacts_root())
    shadow_binding = bindings.read(args.strategy_instance_id)
    twin_binding = bindings.read(args.twin_strategy_instance_id)
    if shadow_binding is None or twin_binding is None:
        absent = args.strategy_instance_id if shadow_binding is None else args.twin_strategy_instance_id
        raise ShadowOperatorRefusal(f"binding not found for {absent}")
    shadow_account_id = shadow_account_id_for_live_account(live_account_id)
    evaluation = evaluate(
        live_account_id=live_account_id,
        shadow_binding=shadow_binding,
        twin_binding=twin_binding,
        twin_account_id=args.twin_account_id,
        required_sessions=required_sessions,
        session_ledger=ShadowSessionLedger(
            artifacts_root=artifacts_root, account_id=shadow_account_id
        ),
        window=ALPACA_LIVE_CAPABILITIES.extended_hours_window,
        now_ms=now_ms,
        shadow_db_path=confined_account_file(artifacts_root, shadow_account_id, DB_FILENAME),
        twin_db_path=confined_account_file(
            args.twin_artifacts_root or artifacts_root, args.twin_account_id, DB_FILENAME
        ),
    )
    summary = _summary(evaluation, now_ms=now_ms)
    if args.operation == "receipt" and evaluation.satisfied:
        summary["receipt_sha256"] = _seal(
            evaluation, artifacts_root=artifacts_root, written_at_ms=now_ms
        ).receipt_sha256
    _write(summary)
    return 0 if evaluation.satisfied or args.operation == "sessions" else 2


def main(argv: list[str] | None = None, *, evaluate: ShadowGateEvaluator = _default_evaluate) -> int:
    try:
        args = _parse_args(argv)
        # Before anything reaches the shadow world: a reserved ``shadow:`` or
        # ``sim:`` id is not a live account, and must never become the subject
        # of an activation or a receipt.
        live_account_id = require_real_account_id(args.live_account_id)
        artifacts_root = args.artifacts_root or get_alpaca_settings().clerk_dir
        if args.operation == "activate":
            return _activate(live_account_id=live_account_id, artifacts_root=artifacts_root)
        return _judge(
            args,
            live_account_id=live_account_id,
            artifacts_root=artifacts_root,
            evaluate=evaluate,
        )
    except ShadowTwinMismatch as exc:
        _write({"error": exc.reason_code, "detail": str(exc)})
        return 2
    except (
        AccountAuthorityIdentityError,
        ShadowOperatorRefusal,
        EconomicProjectionUnavailable,
        # A usage refusal is an input error, not a gate verdict. Reaching it
        # here is what keeps the promise above: one JSON object, always.
        argparse.ArgumentError,
        # The sealer is the last word on the receipt's own shape. Bounded flags
        # make its refusal unreachable from operator input; it is caught anyway
        # so a shape nobody anticipated is still a sentence, not a traceback.
        ShadowReceiptInvalid,
    ) as exc:
        _write({"error": str(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
