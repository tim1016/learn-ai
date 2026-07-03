"""Account-recovery CLI policy helpers.

This module owns the recovery/freeze decisions behind ``run.py`` subcommands.
``run.py`` stays as parser and dispatch glue.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from app.engine.live.desired_state import DesiredState

if TYPE_CHECKING:
    from app.engine.live.account_artifacts import AccountAuditedOverride, AccountRecoveryProof
    from app.services.resume_guard_state import ResumeGuardState

RunDirFinder = Callable[[Path, str], Path | None]
SetDesiredState = Callable[[argparse.Namespace, DesiredState], int]


def latest_run_dir_for_instance(artifacts_root: Path, strategy_instance_id: str) -> Path | None:
    """Find the newest ``live_runs/<run_id>/`` ledger for one strategy instance."""

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
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
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


def account_id_from_run_ledger(run_dir: Path) -> str | None:
    """Return the account id from a run ledger when present and readable."""

    ledger_path = run_dir / "run_ledger.json"
    if not ledger_path.exists():
        return None
    try:
        payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    account_id = payload.get("account_id") if isinstance(payload, dict) else None
    if not isinstance(account_id, str) or not account_id:
        return None
    return account_id


def cmd_resume(
    args: argparse.Namespace,
    *,
    set_desired_state: SetDesiredState,
    run_dir_finder: RunDirFinder = latest_run_dir_for_instance,
) -> int:
    """Set durable ``desired_state=RUNNING`` after account-recovery guards pass."""

    from app.services.resume_guard_state import resolve_guard_state_from_paths

    run_dir = run_dir_finder(args.artifacts_root, args.strategy_instance_id)
    if run_dir is not None:
        account_id = account_id_from_run_ledger(run_dir)
        if account_id is not None:
            freeze_rc = _refuse_if_account_frozen(args.artifacts_root, account_id)
            if freeze_rc is not None:
                return freeze_rc

        guard_state = resolve_guard_state_from_paths(
            verdict_snapshot_path=run_dir / "verdict_snapshot.json",
            run_status_path=run_dir / "run_status.json",
            run_dir_for_reconciliation=run_dir,
            intent_wal_path=run_dir / "intent_events.jsonl",
        )
        if not guard_state.allow_resume:
            print(_resume_guard_refusal_message(guard_state), file=sys.stderr)
            return 2

    return set_desired_state(args, DesiredState.RUNNING)


def cmd_clear_account_freeze(args: argparse.Namespace) -> int:
    """Clear an account freeze through the shipped operator CLI path."""

    if not args.confirm:
        print(
            "[CLEAR-FREEZE] REFUSED: pass --confirm after reviewing the recovery evidence.",
            file=sys.stderr,
        )
        return 2

    from app.engine.live.account_artifacts import AccountArtifactError, clear_account_freeze

    try:
        if args.recovery_proof_json is not None:
            recovery_proof = _load_recovery_proof_json(args.recovery_proof_json)
            clear_account_freeze(args.artifacts_root, recovery_proof=recovery_proof)
            account_id = recovery_proof.account_id
            source = f"recovery:{recovery_proof.recovery_id}"
        else:
            audited_override = _load_audited_override_json(args.audited_override_json)
            clear_account_freeze(args.artifacts_root, audited_override=audited_override)
            account_id = audited_override.account_id
            source = f"override:{audited_override.override_id}"
    except (AccountArtifactError, ValueError) as exc:
        print(f"[CLEAR-FREEZE] REFUSED: {exc}", file=sys.stderr)
        return 2

    print(f"[CLEAR-FREEZE] cleared account {account_id} via {source}")
    return 0


def _refuse_if_account_frozen(artifacts_root: Path, account_id: str) -> int | None:
    from app.engine.live.account_artifacts import read_account_freeze

    try:
        account_freeze = read_account_freeze(artifacts_root, account_id)
    except (OSError, ValueError) as exc:
        print(
            f"[RESUME] REFUSED (ACCOUNT_FREEZE_UNKNOWN): "
            f"could not read account freeze state for {account_id}: {exc}",
            file=sys.stderr,
        )
        return 2
    if account_freeze is None:
        return None
    print(
        f"[RESUME] REFUSED (ACCOUNT_FREEZE_ACTIVE): "
        f"account {account_id} is frozen: {account_freeze.reason}; "
        f"next step: {account_freeze.operator_next_step}",
        file=sys.stderr,
    )
    return 2


def _resume_guard_refusal_message(guard_state: ResumeGuardState) -> str:
    head = guard_state.reason_codes[0]
    all_codes = ", ".join(guard_state.reason_codes)
    broker_v = guard_state.broker_safety.verdict
    wal_pending = guard_state.uncertain_intent.unresolved_intent_ids
    details: list[str] = []
    if guard_state.broker_safety.state != "SAFE":
        details.append(f"broker-safety={guard_state.broker_safety.state!s} verdict={broker_v!r}")
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
    return (
        f"[RESUME] REFUSED ({head}): "
        + "; ".join(details)
        + f"\n  all applicable reason codes: {all_codes}"
        + "\n  resolve the underlying condition; --force was removed in PRD #616."
    )


def _load_recovery_proof_json(path: Path) -> AccountRecoveryProof:
    from pydantic import ValidationError

    from app.engine.live.account_artifacts import AccountRecoveryProof

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return AccountRecoveryProof.model_validate(payload)
    except OSError as exc:
        raise ValueError(f"could not read recovery proof JSON at {path}: {exc}") from exc
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"invalid recovery proof JSON at {path}: {exc}") from exc


def _load_audited_override_json(path: Path) -> AccountAuditedOverride:
    from pydantic import ValidationError

    from app.engine.live.account_artifacts import AccountAuditedOverride

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return AccountAuditedOverride.model_validate(payload)
    except OSError as exc:
        raise ValueError(f"could not read audited override JSON at {path}: {exc}") from exc
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"invalid audited override JSON at {path}: {exc}") from exc


__all__ = [
    "account_id_from_run_ledger",
    "cmd_clear_account_freeze",
    "cmd_resume",
    "latest_run_dir_for_instance",
]
