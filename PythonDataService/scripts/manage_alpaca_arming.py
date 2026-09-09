"""Operator CLI for the arming ceremony (ADR 0059 D3): status, plan, apply, disarm.

``status`` judges one instance, or every instance with a row in the ledger, and
writes nothing. ``plan`` re-observes every input and prints a read-only proposal
whose content hash is its own confirmation token. ``apply`` re-observes again,
refuses any drift, and appends the sealed arming record -- the one write on this
path. ``disarm`` appends a revocation; it is the closed direction and takes no
plan.

The live account is never supplied on the command line: it is observed from the
shadow activation fence under ``--artifacts-root``, so an arming can only name an
account a shadow gate was actually run against.

Exit codes: ``0`` the command answered; ``1`` the command cannot be run as asked
-- a plan file that is not one, a ledger row that will not verify, or a usage
refusal (an absent flag, an unknown subcommand, a flag outside its bound); ``2``
the ceremony refused, under a named ``LIVE_ARMING_*`` / ``LIVE_SHADOW_INCOMPLETE``
/ ``LIVE_ENVELOPE_MISSING`` code.

Every invocation writes exactly one JSON object to stdout, every temporal value
in it is ``int64 ms UTC``, and every object carries ``"submission_admitted":
false`` -- the fact an operator most easily assumes wrong. (``--help`` is
argparse's own usage text and exits ``0``; it runs no command.)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.account_authority import AccountAuthorityIdentityError
from app.broker.alpaca.clerk.ceremony import DEFAULT_CONFIRMATION_TTL_MS, MAX_CONFIRMATION_TTL_MS
from app.broker.alpaca.clerk.live_arming import (
    LIVE_ENVELOPE_MISSING,
    ArmingStatus,
    LiveArmingInvalid,
    LiveArmingRefused,
)
from app.broker.alpaca.clerk.live_arming_ceremony import (
    LiveArmingPlan,
    account_arming_statuses,
    apply_arming,
    disarm,
    live_account_id_for,
    plan_arming,
)
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeIncomplete, LiveEnvelopeValues
from app.broker.alpaca.clerk.shadow_activation import ShadowActivationInvalid
from app.broker.alpaca.clerk.shadow_receipt import ShadowReceiptInvalid
from app.broker.alpaca.clerk.sqlite.operational_files import atomic_write_json
from app.broker.alpaca.config import AlpacaSettings, get_alpaca_settings
from app.broker.ibkr.config import live_artifacts_root
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from app.utils.timestamps import Clock, now_ms_utc

SUBMISSION_NOTE = (
    "An arming record is evidence, not a submission path: no code path submits a "
    "real-money order in ADR 0059 slice 6. Slice 7 is what reads this record at "
    "ENTER admission."
)


class ArmingOperatorRefusal(ValueError):
    """This command cannot be run as asked -- a named input is absent or malformed."""


def _timestamp_ms(raw: str) -> int:
    """One instant, ``int64 ms UTC``, inside the domain's admissible range."""
    value = int(raw)
    if not 0 <= value <= MAX_TIMESTAMP_MS:
        raise argparse.ArgumentTypeError(
            f"must be between 0 and {MAX_TIMESTAMP_MS} milliseconds since epoch UTC, not {value}"
        )
    return value


def _confirmation_ttl_ms(raw: str) -> int:
    """The same bound the ceremony enforces, on the flag that carries it."""
    value = int(raw)
    if not 1 <= value <= MAX_CONFIRMATION_TTL_MS:
        raise argparse.ArgumentTypeError(
            f"must be between 1 and {MAX_CONFIRMATION_TTL_MS} milliseconds, not {value}"
        )
    return value


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    # ``exit_on_error=False`` on this parser *and* on every subparser: without it
    # argparse writes usage to stderr and exits 2 on its own, which is the code
    # "the ceremony refused" already owns here.
    parser = argparse.ArgumentParser(
        prog="scripts.manage_alpaca_arming",
        description="Arm, disarm and inspect sealed instances on the shadowed live account.",
        exit_on_error=False,
    )
    parser.add_argument("--artifacts-root", type=Path)
    parser.add_argument("--live-state-root", type=Path)
    subparsers = parser.add_subparsers(dest="operation", required=True)

    status = subparsers.add_parser(
        "status", help="Report one instance, or every instance with a record.", exit_on_error=False
    )
    status.add_argument("--strategy-instance-id")

    planner = subparsers.add_parser(
        "plan", help="Re-observe every input and print a read-only arming plan.", exit_on_error=False
    )
    planner.add_argument("--strategy-instance-id", required=True)
    planner.add_argument("--confirmation-ttl-ms", type=_confirmation_ttl_ms, default=DEFAULT_CONFIRMATION_TTL_MS)
    planner.add_argument("--plan-out", type=Path)

    applier = subparsers.add_parser(
        "apply", help="Re-observe, refuse drift, and append the sealed arming record.", exit_on_error=False
    )
    applier.add_argument("--plan-file", type=Path, required=True)
    applier.add_argument("--confirmation-token", required=True)

    disarmer = subparsers.add_parser(
        "disarm", help="Append a revocation for one armed instance.", exit_on_error=False
    )
    disarmer.add_argument("--strategy-instance-id", required=True)

    for subparser in (status, planner, applier, disarmer):
        subparser.add_argument("--now-ms", type=_timestamp_ms)
    return parser.parse_args(argv)


def _write(payload: Mapping[str, Any]) -> None:
    """One JSON object per invocation, on stdout.

    Every object carries ``submission_admitted`` and its note because that is
    the fact an operator most easily assumes wrong: arming is a permission slice
    7 will read, not a submission path this slice opened. ``default=str`` covers
    the ``Path`` values a plan may carry; every temporal value is already
    ``int64 ms UTC``.
    """
    sys.stdout.write(
        json.dumps(
            {**payload, "submission_admitted": False, "note": SUBMISSION_NOTE},
            sort_keys=True,
            default=str,
        )
        + "\n"
    )


def _clock(now_ms: int | None) -> Clock:
    return now_ms_utc if now_ms is None else (lambda: now_ms)


def _configured_envelope(settings: AlpacaSettings) -> LiveEnvelopeValues:
    """The environment's current envelope, refused by the ceremony's own code."""
    if settings.mode != "live":
        raise LiveArmingRefused(
            LIVE_ENVELOPE_MISSING,
            f"ALPACA_MODE={settings.mode}; arming is a live-account question (ADR 0059 D3).",
        )
    try:
        return LiveEnvelopeValues.from_settings(settings)
    except LiveEnvelopeIncomplete as exc:
        raise LiveArmingRefused(LIVE_ENVELOPE_MISSING, str(exc)) from exc


def _read_plan(path: Path) -> LiveArmingPlan:
    """The plan file, or a sentence naming what is wrong with it.

    ``submission_admitted`` and ``note`` are stripped so a plan an operator
    saved from stdout reads back exactly like one written by ``--plan-out``.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArmingOperatorRefusal(f"arming plan file is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise ArmingOperatorRefusal("arming plan file must contain a JSON object")
    payload.pop("submission_admitted", None)
    payload.pop("note", None)
    try:
        return LiveArmingPlan(**payload)
    except TypeError as exc:
        raise ArmingOperatorRefusal(f"arming plan file is not an arming plan: {exc}") from exc


def _instance_payload(status: ArmingStatus, *, strategy_instance_id: str) -> dict[str, Any]:
    record = status.record
    return {
        "strategy_instance_id": strategy_instance_id,
        "state": status.state,
        "reason_code": status.reason_code,
        "sessions_used": status.sessions_used,
        "sessions_remaining": status.sessions_remaining,
        "armed_at_ms": None if record is None else record.armed_at_ms,
        "max_sessions": None if record is None else record.max_sessions,
        "seal_hash": None if record is None else record.seal_hash,
        "envelope_sha256": None if record is None else record.envelope_sha256,
        "record_sha256": None if record is None else record.record_sha256,
    }


def _status(
    args: argparse.Namespace, *, artifacts_root: Path, live_state_root: Path, settings: AlpacaSettings
) -> int:
    now_ms = now_ms_utc() if args.now_ms is None else args.now_ms
    live_account_id = live_account_id_for(artifacts_root)
    statuses = account_arming_statuses(
        live_account_id=live_account_id,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        configured_envelope=_configured_envelope(settings),
        now_ms=now_ms,
        strategy_instance_ids=(
            None if args.strategy_instance_id is None else [args.strategy_instance_id]
        ),
    )
    sealed = LiveArmingLedger(artifacts_root, live_account_id=live_account_id).latest_arming()
    _write(
        {
            "now_ms": now_ms,
            "live_account_id": live_account_id,
            "envelope_state": "configured_unsealed" if sealed is None else "sealed",
            "armed_instance_count": sum(1 for status in statuses.values() if status.state == "armed"),
            "instances": [
                _instance_payload(status, strategy_instance_id=sid)
                for sid, status in sorted(statuses.items())
            ],
        }
    )
    return 0


def _plan(
    args: argparse.Namespace, *, artifacts_root: Path, live_state_root: Path, settings: AlpacaSettings
) -> int:
    plan = plan_arming(
        strategy_instance_id=args.strategy_instance_id,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=settings,
        confirmation_ttl_ms=args.confirmation_ttl_ms,
        clock=_clock(args.now_ms),
    )
    if args.plan_out is not None:
        atomic_write_json(args.plan_out, asdict(plan))
    _write(asdict(plan))
    return 0


def _apply(
    args: argparse.Namespace, *, artifacts_root: Path, live_state_root: Path, settings: AlpacaSettings
) -> int:
    record = apply_arming(
        plan=_read_plan(args.plan_file),
        confirmation_token=args.confirmation_token,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=settings,
        clock=_clock(args.now_ms),
    )
    _write(asdict(record))
    return 0


def _disarm(args: argparse.Namespace, *, artifacts_root: Path) -> int:
    _write(
        asdict(
            disarm(
                strategy_instance_id=args.strategy_instance_id,
                artifacts_root=artifacts_root,
                clock=_clock(args.now_ms),
            )
        )
    )
    return 0


def main(argv: list[str] | None = None, *, settings: AlpacaSettings | None = None) -> int:
    try:
        args = _parse_args(argv)
        resolved = get_alpaca_settings() if settings is None else settings
        artifacts_root = args.artifacts_root or resolved.clerk_dir
        live_state_root = args.live_state_root or live_artifacts_root()
        if args.operation == "status":
            return _status(
                args, artifacts_root=artifacts_root, live_state_root=live_state_root, settings=resolved
            )
        if args.operation == "plan":
            return _plan(
                args, artifacts_root=artifacts_root, live_state_root=live_state_root, settings=resolved
            )
        if args.operation == "apply":
            return _apply(
                args, artifacts_root=artifacts_root, live_state_root=live_state_root, settings=resolved
            )
        return _disarm(args, artifacts_root=artifacts_root)
    except LiveArmingRefused as exc:
        _write({"error": exc.reason_code, "detail": str(exc)})
        return 2
    except (
        ArmingOperatorRefusal,
        # The sealed stores are the last word on their own rows; their refusal
        # is a sentence for an operator, never a traceback.
        LiveArmingInvalid,
        ShadowActivationInvalid,
        ShadowReceiptInvalid,
        AccountAuthorityIdentityError,
        # A usage refusal is an input error, not a ceremony verdict. Reaching it
        # here is what keeps the promise above: one JSON object, always.
        argparse.ArgumentError,
    ) as exc:
        _write({"error": str(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
