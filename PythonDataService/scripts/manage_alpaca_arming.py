"""Operator CLI for the arming ceremony (ADR 0059 D3): status, plan, apply, disarm.

``status`` judges one instance, or every instance with a row in the ledger, and
writes nothing. ``plan`` re-observes every input and prints a read-only proposal
whose content hash is its own confirmation token. ``apply`` re-observes again,
refuses any drift, and appends the sealed arming record -- the one write on this
path. ``disarm`` appends a revocation; it is the closed direction and takes no
plan.

The live account is never supplied on the command line. ``plan`` and ``apply``
observe it from the instance's sealed binding: a Shadow-bound instance must
prove its shadow activation fence, while a Live-bound instance must prove its
verified cutover activation. Read-only ``status`` may recover a graduated
account from its activation or a previously armed instance from its unique
ledger row. ``disarm`` may also read the ledger that already names the instance.

The envelope this ceremony seals comes from the installation's **effective**
profile revision (ADR 0060), never from the process environment and never from
a merely staged revision: ``plan`` and ``apply`` refuse while a *different*
revision is staged, naming both, because sealing what the running worker is not
enforcing would put the seal in disagreement the moment the Apply lands (owner
decisions D3 and D5). ``plan`` prints the before→after difference between the
envelope it would seal and the one currently sealed on the account, which is the
operator's last look before a real-money limit changes. This module also owns
:func:`effective_broker` -- the one resolver the three ``manage_alpaca_*``
operator CLIs share, because each runs in its own process where no worker has
installed a binding.

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

from app.broker.alpaca.active_binding import (
    BrokerUnbound,
)
from app.broker.alpaca.clerk.account_authority import AccountAuthorityIdentityError
from app.broker.alpaca.clerk.ceremony import DEFAULT_CONFIRMATION_TTL_MS, MAX_CONFIRMATION_TTL_MS
from app.broker.alpaca.clerk.live_arming import (
    LIVE_ENVELOPE_MISSING,
    ArmingStatus,
    LiveArmingInvalid,
    LiveArmingRefused,
    latest_arming,
)
from app.broker.alpaca.clerk.live_arming_ceremony import (
    LiveArmingPlan,
    account_arming,
    apply_arming,
    configured_envelope,
    disarm,
    live_account_activation_is_verified,
    live_account_id_for_status,
    plan_arming,
)
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.live_envelope import ENVELOPE_SETTINGS_FIELDS
from app.broker.alpaca.clerk.shadow_activation import ShadowActivationInvalid
from app.broker.alpaca.clerk.shadow_receipt import ShadowReceiptInvalid
from app.broker.alpaca.clerk.sqlite.activation import ActivationRecordInvalid
from app.broker.alpaca.clerk.sqlite.operational_files import atomic_write_json
from app.broker.alpaca.config import (
    AlpacaSettings,
    alpaca_configuration_error_detail,
)
from app.broker.ibkr.config import live_artifacts_root
from app.broker_configuration.cli_binding import (
    EffectiveBroker,
    effective_broker,
)
from app.broker_configuration.records import InstallationSelection
from app.broker_configuration.selection import reference as revision_reference
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
    "the live activation evidence or Clerk database for this account does not verify; nothing "
    "submits until an operator repairs it (ADR 0059 D1)"
)

# This ceremony arms the effective revision only (ADR 0060 owner decisions D3
# and D5). The code lives here rather than beside the ``LIVE_ARMING_*`` codes in
# ``clerk/live_arming.py`` because the rule is this command's, not the seal's:
# the ceremony knows about envelopes and instances and nothing at all about
# profile revisions, and a code it can never raise does not belong in its
# vocabulary. It is still a *typed* refusal in this module's own contract --
# ``LiveArmingRefused`` carrying a reason code, printed as one JSON object and
# exiting 2, exactly like every other refused ceremony.
LIVE_ARMING_REVISION_STAGED = "LIVE_ARMING_REVISION_STAGED"

# The six envelope fields, in the canonical order ``LiveEnvelopeValues``
# declares them -- taken from the same pairing the sha is built over rather
# than hand-listed, so a seventh value cannot go unreported in a diff.
_ENVELOPE_FIELD_ORDER: tuple[str, ...] = tuple(field for field, _ in ENVELOPE_SETTINGS_FIELDS)

# Keys ``_write`` adds to a printed payload that are *reports about* the plan
# rather than part of it. ``_read_plan`` strips them so a plan an operator
# saved from stdout still reads back as a plan.
_PLAN_REPORT_KEYS: tuple[str, ...] = ("submission_admitted", "note", "envelope_change")


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
    planner.add_argument(
        "--predecessor-strategy-instance-id",
        help="the sealed Shadow rehearsal instance this new Live instance succeeds",
    )
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


def _submission_admitted(
    artifacts_root: Path | None, live_account_id: str | None
) -> tuple[bool, str]:
    """Whether a live authority is activated for this account.

    Since ADR 0059 slice 7, an armed instance's ENTER is only ever submitted
    once graduation -- the live cutover -- has installed the live authority
    for the account; arming alone never opens that path. The cutover's own
    The ceremony's shared activation verifier is the read of that fact. Without
    a resolvable ``artifacts_root`` or ``live_account_id`` (a usage or
    pre-dispatch refusal) account resolution never happened, so admission was
    never evaluated -- that is a distinct fact from an ungraduated account and
    gets its own note. Activation, cutover-artifact, containment, or database
    verification failure is reported the same way: not admitted, with a note
    naming the unverified authority instead of a traceback.

    The id is a parameter, not a probe into the payload: every caller that
    has one already holds it, and a payload that came to name it differently
    would degrade silently to "not evaluated".
    """
    if artifacts_root is None or live_account_id is None:
        return False, _SUBMISSION_UNEVALUATED_NOTE
    try:
        activated = live_account_activation_is_verified(
            live_account_id=live_account_id,
            artifacts_root=artifacts_root,
        )
    except ActivationRecordInvalid as exc:
        logger.warning(
            "the live authority evidence does not verify; submission is reported as not admitted",
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


def _write(
    payload: Mapping[str, Any],
    *,
    artifacts_root: Path | None = None,
    live_account_id: str | None = None,
) -> None:
    """One JSON object per invocation, on stdout.

    Every object carries a computed ``submission_admitted`` and its note
    because that is the fact an operator most easily assumes wrong: arming
    alone never opens the submission path -- graduation does (see
    ``_submission_admitted``). ``default=str`` covers the ``Path`` values a
    plan may carry; every temporal value is already ``int64 ms UTC``.
    """
    submission_admitted, note = _submission_admitted(artifacts_root, live_account_id)
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


def _resolved_broker(supplied: AlpacaSettings | None) -> EffectiveBroker:
    """The effective revision's Alpaca settings, refused by the ceremony's own code.

    Two failures have to become one named refusal here rather than escape as an
    exception, because both land exactly where this module's contract (one JSON
    object per invocation) says a named refusal fires.

    The first is the pre-cutover bootstrap's: ``AlpacaSettings`` refuses to
    construct at all when ``ALPACA_MODE=live`` and any ``ALPACA_LIVE_*`` value is
    absent -- the single most likely real misconfiguration on a live account.
    The second is a refused binding: a configured installation that has applied
    nothing, an unreadable profiles database, or an effective revision that will
    not resolve. Both are "the configuration this ceremony needs is not there",
    which is what ``LIVE_ENVELOPE_MISSING`` already says; the detail names the
    contract's own reason code so the operator sees which of the two it was.
    """
    if supplied is not None:
        return EffectiveBroker(settings=supplied, selection=None)
    try:
        return effective_broker()
    except ValidationError as exc:
        raise LiveArmingRefused(
            LIVE_ENVELOPE_MISSING,
            f"{alpaca_configuration_error_detail(exc)} Pass --artifacts-root to run "
            "disarm without a loadable environment.",
        ) from exc
    except BrokerUnbound as exc:
        raise LiveArmingRefused(
            LIVE_ENVELOPE_MISSING,
            f"{exc.reason}: {exc.unbound.message} {exc.unbound.next_step} "
            "Pass --artifacts-root to run disarm without a resolvable binding.",
        ) from exc


def _require_arming_the_effective_revision(selection: InstallationSelection | None) -> None:
    """Arm the effective revision only, never a merely staged one (D3, D5).

    A staged revision governs nothing until the operator presses Apply and the
    worker binds it. Sealing one anyway would put the arming record in
    ``LIVE_ENVELOPE_DISAGREEMENT`` against the envelope the running worker is
    actually enforcing -- and would look armed until it did. The refusal names
    both revisions, because the operator's next question is always which of the
    two they were looking at.

    ``status`` and ``disarm`` are deliberately not gated: reading the state and
    revoking a permission are exactly what an operator needs while a change is
    pending, and neither seals anything.
    """
    if selection is None:
        return
    # Both halves, the same guard ``binding_decision.decide`` applies: a row
    # naming no exact revision is not a staged revision to disagree with.
    if selection.staged_profile_id is None or selection.staged_revision is None:
        return
    staged = (selection.staged_profile_id, selection.staged_revision)
    effective = (selection.effective_profile_id, selection.effective_revision)
    if staged == effective:
        return
    raise LiveArmingRefused(
        LIVE_ARMING_REVISION_STAGED,
        f"broker configuration {revision_reference(*staged)} is staged but "
        f"{revision_reference(*effective)} is effective; this ceremony arms the effective "
        "revision only. Apply the staged revision and restart the service, or stage the "
        "effective one again, then plan.",
    )


def _envelope_change(plan: LiveArmingPlan, *, artifacts_root: Path) -> dict[str, Any]:
    """What applying this plan does to the envelope sealed on this account.

    The operator's last look before a real-money limit changes, so the answer is
    stated twice: one ``summary`` sentence naming every value that moves, and a
    machine-readable ``changes`` list of before→after pairs. Values are rendered
    with ``repr`` on purpose -- ``5000`` and ``5000.0`` are different documents
    to the envelope sha, so a diff that hid the difference would hide a real
    change.

    "The currently sealed envelope" is an *account-level* fact: the newest
    arming record on the account, which is the same record ``status`` reports as
    ``envelope_state`` (R11), not this instance's own last arming. ``sealed_by``
    names it so there is no ambiguity about what the comparison was against.
    """
    sealed = latest_arming(
        LiveArmingLedger(artifacts_root, live_account_id=plan.live_account_id).records()
    )
    after = plan.envelope_values
    if sealed is None:
        return {
            "changed": True,
            "sealed_by": None,
            "changes": [
                {"field": field, "before": None, "after": after[field]}
                for field in _ENVELOPE_FIELD_ORDER
            ],
            "summary": (
                "FIRST SEAL: no arming record has sealed this account's envelope yet, so this "
                "plan seals all six live envelope values: "
                + "; ".join(f"{field} = {after[field]!r}" for field in _ENVELOPE_FIELD_ORDER)
                + "."
            ),
        }

    before = sealed.envelope.to_mapping()
    changes = [
        {"field": field, "before": before[field], "after": after[field]}
        for field in _ENVELOPE_FIELD_ORDER
        if before[field] != after[field]
    ]
    sealed_by = {
        "strategy_instance_id": sealed.strategy_instance_id,
        "armed_at_ms": sealed.armed_at_ms,
        "envelope_sha256": sealed.envelope_sha256,
    }
    if not changes:
        return {
            "changed": False,
            "sealed_by": sealed_by,
            "changes": [],
            "summary": (
                "NO CHANGE: all six live envelope values are exactly the ones already sealed on "
                f"this account by {sealed.strategy_instance_id} (envelope {sealed.envelope_sha256})."
            ),
        }
    return {
        "changed": True,
        "sealed_by": sealed_by,
        "changes": changes,
        "summary": (
            f"CHANGED: {len(changes)} of the 6 live envelope values differ from the envelope "
            f"sealed on this account by {sealed.strategy_instance_id}: "
            + "; ".join(
                f"{change['field']} {change['before']!r} -> {change['after']!r}"
                for change in changes
            )
            + "."
        ),
    }


def _read_plan(path: Path) -> LiveArmingPlan:
    """The plan file, or a sentence naming what is wrong with it.

    Every key ``_write`` adds *about* a plan -- ``submission_admitted``,
    ``note`` and the ``envelope_change`` report -- is stripped so a plan an
    operator saved from stdout reads back exactly like one written by
    ``--plan-out``.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArmingOperatorRefusal(f"arming plan file is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise ArmingOperatorRefusal("arming plan file must contain a JSON object")
    for key in _PLAN_REPORT_KEYS:
        payload.pop(key, None)
    try:
        return LiveArmingPlan.from_payload(payload)
    except (TypeError, ValueError) as exc:
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
        "predecessor": None if record is None or record.predecessor is None else asdict(record.predecessor),
        "originating_plan_id": None if record is None else record.originating_plan_id,
    }


def _status(
    args: argparse.Namespace, *, artifacts_root: Path, live_state_root: Path, settings: AlpacaSettings
) -> int:
    now_ms = now_ms_utc() if args.now_ms is None else args.now_ms
    live_account_id = live_account_id_for_status(
        strategy_instance_id=args.strategy_instance_id,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
    )
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
        live_account_id=live_account_id,
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
        predecessor_strategy_instance_id=args.predecessor_strategy_instance_id,
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
    # The diff is a report *about* the plan, so it is printed beside it and
    # never written into ``--plan-out``: the plan's confirmation token is its
    # own content hash, and a plan file carrying an extra key would not be one.
    _write(
        {**asdict(plan), "envelope_change": _envelope_change(plan, artifacts_root=artifacts_root)},
        artifacts_root=artifacts_root,
        live_account_id=plan.live_account_id,
    )
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
    _write(asdict(record), artifacts_root=artifacts_root, live_account_id=record.live_account_id)
    return 0


def _disarm(args: argparse.Namespace, *, artifacts_root: Path) -> int:
    record = disarm(
        strategy_instance_id=args.strategy_instance_id,
        artifacts_root=artifacts_root,
        clock=_clock(args.now_ms),
    )
    _write(asdict(record), artifacts_root=artifacts_root, live_account_id=record.live_account_id)
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
                artifacts_root=args.artifacts_root or _resolved_broker(settings).settings.clerk_dir,
            )
        resolved = _resolved_broker(settings)
        artifacts_root = args.artifacts_root or resolved.settings.clerk_dir
        live_state_root = args.live_state_root or live_artifacts_root()
        if args.operation == "status":
            return _status(
                args,
                artifacts_root=artifacts_root,
                live_state_root=live_state_root,
                settings=resolved.settings,
            )
        # Only the two writing directions are gated: sealing an envelope the
        # running worker is not enforcing is the outcome D3 and D5 forbid.
        _require_arming_the_effective_revision(resolved.selection)
        if args.operation == "plan":
            return _plan(
                args,
                artifacts_root=artifacts_root,
                live_state_root=live_state_root,
                settings=resolved.settings,
            )
        return _apply(
            args,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            settings=resolved.settings,
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
