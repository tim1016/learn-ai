"""Operator CLI for the arming ceremony (ADR 0059 D3): status, plan, apply, disarm.

``status`` judges one instance, or every instance with a row in the ledger, and
writes nothing. ``plan`` re-observes every input and prints a read-only proposal
whose content hash is its own confirmation token. ``apply`` re-observes again,
refuses any drift, and appends the sealed arming record -- the one write on this
path. ``disarm`` appends a revocation; it is the closed direction and takes no
plan.

The live account is never supplied on the command line: it is observed from the
shadow activation fence under ``--artifacts-root`` (and, for ``disarm`` alone
when that fence is gone or ambiguous, from the arming ledger that already
names the instance), so an arming can only name an account a shadow gate was
actually run against. ``plan``, ``apply`` and ``status`` still observe only
the fence.

Exit codes: ``0`` the command answered; ``1`` the command cannot be run as asked
-- a plan file that is not one, a ledger row that will not verify, or a usage
refusal (an absent flag, an unknown subcommand, a flag outside its bound); ``2``
the ceremony refused, under a named ``LIVE_ARMING_*`` / ``LIVE_ENVELOPE_MISSING``
code.

Every invocation writes exactly one JSON object to stdout, every temporal value
in it is ``int64 ms UTC``, and every object carries a computed
``submission_admitted`` and ``note``: since ADR 0059 slice 7, whether an armed
instance's ENTER is actually submitted depends on graduation -- whether a live
authority is activated for this account -- not on this ceremony alone.
(``--help`` is argparse's own usage text and exits ``0``; it runs no command.)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic import ValidationError

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
    account_arming,
    apply_arming,
    configured_envelope,
    disarm,
    live_account_id_for,
    plan_arming,
)
from app.broker.alpaca.clerk.shadow_activation import ShadowActivationInvalid
from app.broker.alpaca.clerk.shadow_receipt import ShadowReceiptInvalid
from app.broker.alpaca.clerk.sqlite.activation import ActivationRecordInvalid, ActivationStore
from app.broker.alpaca.clerk.sqlite.operational_files import atomic_write_json
from app.broker.alpaca.config import (
    AlpacaSettings,
    alpaca_configuration_error_detail,
    get_alpaca_settings,
)
from app.broker.ibkr.config import live_artifacts_root
from app.utils.timestamps import Clock, now_ms_utc
from scripts._operator_cli import timestamp_ms

logger = logging.getLogger(__name__)

_SUBMISSION_ADMITTED_NOTE = (
    "a live activation record exists for this account; an armed instance's ENTER is submitted "
    "once the live authority boots — and it refuses to boot behind "
    "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL=true (ADR 0059 D10, slice 7 R14)"
)
_SUBMISSION_NOT_ADMITTED_NOTE = (
    "no live authority is activated for this account; nothing submits until the live cutover"
)
_SUBMISSION_UNEVALUATED_NOTE = (
    "submission admission was not evaluated: the command was refused before its account was resolved"
)
_SUBMISSION_UNVERIFIED_NOTE = (
    "the live activation record for this account does not verify; nothing submits until an operator "
    "repairs it (ADR 0059 D1)"
)


class ArmingOperatorRefusal(ValueError):
    """This command cannot be run as asked -- a named input is absent or malformed."""


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
        subparser.add_argument("--now-ms", type=timestamp_ms)
    return parser.parse_args(argv)


def _submission_admitted(artifacts_root: Path | None, payload: Mapping[str, Any]) -> tuple[bool, str]:
    """Whether a live authority is activated for this payload's account.

    Since ADR 0059 slice 7, an armed instance's ENTER is only ever submitted
    once graduation -- the live cutover -- has installed the live authority
    for the account; arming alone never opens that path. The cutover's own
    ``ActivationStore`` is the read of that fact. A payload with no resolvable
    ``artifacts_root`` or ``live_account_id`` (a usage or pre-dispatch
    refusal) never reached account resolution, so admission was never
    evaluated -- that is a distinct fact from an ungraduated account and gets
    its own note. A ledger that fails ``ActivationStore``'s own verification
    (a symlinked or non-regular file, a non-monotonic generation, malformed
    JSON) is reported the same way: not admitted, with a note naming the
    unverified ledger instead of a traceback.
    """
    live_account_id = payload.get("live_account_id")
    if artifacts_root is None or not isinstance(live_account_id, str):
        return False, _SUBMISSION_UNEVALUATED_NOTE
    try:
        activated = (
            ActivationStore(artifacts_root / "accounts" / "alpaca").latest(live_account_id) is not None
        )
    except ActivationRecordInvalid as exc:
        logger.warning(
            "the live activation record does not verify; submission is reported as not admitted",
            extra={
                "action": "arming_cli_activation_record_invalid",
                "live_account_id": live_account_id,
                "error": str(exc),
            },
        )
        return False, _SUBMISSION_UNVERIFIED_NOTE
    return (
        (True, _SUBMISSION_ADMITTED_NOTE) if activated else (False, _SUBMISSION_NOT_ADMITTED_NOTE)
    )


def _write(payload: Mapping[str, Any], *, artifacts_root: Path | None = None) -> None:
    """One JSON object per invocation, on stdout.

    Every object carries a computed ``submission_admitted`` and its note
    because that is the fact an operator most easily assumes wrong: arming
    alone never opens the submission path -- graduation does (see
    ``_submission_admitted``). ``default=str`` covers the ``Path`` values a
    plan may carry; every temporal value is already ``int64 ms UTC``.
    """
    submission_admitted, note = _submission_admitted(artifacts_root, payload)
    sys.stdout.write(
        json.dumps(
            {**payload, "submission_admitted": submission_admitted, "note": note},
            sort_keys=True,
            default=str,
        )
        + "\n"
    )


def _clock(now_ms: int | None) -> Clock:
    return now_ms_utc if now_ms is None else (lambda: now_ms)


def _resolved_settings(supplied: AlpacaSettings | None) -> AlpacaSettings:
    """The environment's Alpaca settings, refused by the ceremony's own code.

    ``AlpacaSettings`` refuses to construct at all when ``ALPACA_MODE=live`` and
    any ``ALPACA_LIVE_*`` value is absent -- the single most likely real
    misconfiguration on a live account. Letting that ``ValidationError`` escape
    would break this module's contract (one JSON object per invocation) exactly
    where its named refusal was supposed to fire, so it is translated here into
    the ceremony's own ``LIVE_ENVELOPE_MISSING``.
    """
    if supplied is not None:
        return supplied
    try:
        return get_alpaca_settings()
    except ValidationError as exc:
        raise LiveArmingRefused(
            LIVE_ENVELOPE_MISSING,
            f"{alpaca_configuration_error_detail(exc)} Pass --artifacts-root to run "
            "disarm without a loadable environment.",
        ) from exc


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
    arming = account_arming(
        live_account_id=live_account_id,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        configured_envelope=configured_envelope(settings),
        now_ms=now_ms,
        strategy_instance_ids=(
            None if args.strategy_instance_id is None else [args.strategy_instance_id]
        ),
    )
    _write(
        {
            "now_ms": now_ms,
            "live_account_id": live_account_id,
            "envelope_state": arming.envelope_state,
            "armed_instance_count": arming.armed_instance_count,
            "instances": [
                _instance_payload(status, strategy_instance_id=sid)
                for sid, status in sorted(arming.statuses.items())
            ],
        },
        artifacts_root=artifacts_root,
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
        # An unwritable ``--plan-out`` -- a path whose parent is a regular file,
        # a read-only tree, a failed replace or fsync -- must not replace this
        # module's one-JSON-object contract with a traceback. The refusal is
        # raised before anything is printed, so the operator reads exactly one
        # object and it is the error.
        try:
            atomic_write_json(args.plan_out, asdict(plan))
        except OSError as exc:
            raise ArmingOperatorRefusal(f"cannot write the plan file: {exc}") from exc
    _write(asdict(plan), artifacts_root=artifacts_root)
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
    _write(asdict(record), artifacts_root=artifacts_root)
    return 0


def _disarm(args: argparse.Namespace, *, artifacts_root: Path) -> int:
    _write(
        asdict(
            disarm(
                strategy_instance_id=args.strategy_instance_id,
                artifacts_root=artifacts_root,
                clock=_clock(args.now_ms),
            )
        ),
        artifacts_root=artifacts_root,
    )
    return 0


def main(argv: list[str] | None = None, *, settings: AlpacaSettings | None = None) -> int:
    try:
        args = _parse_args(argv)
        # Settings are resolved per subcommand, never before dispatch: ``disarm``
        # is the closed direction and reads no settings, no binding and no
        # receipt (R4), so an operator can revoke an arming whose environment
        # has since been half-edited -- which is the same incident. Only the
        # default artifacts root needs the environment, and ``--artifacts-root``
        # supplies it.
        if args.operation == "disarm":
            return _disarm(
                args,
                artifacts_root=args.artifacts_root or _resolved_settings(settings).clerk_dir,
            )
        resolved = _resolved_settings(settings)
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
        return _apply(
            args, artifacts_root=artifacts_root, live_state_root=live_state_root, settings=resolved
        )
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
