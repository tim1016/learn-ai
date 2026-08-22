"""Strict, read-only runner evidence for an Alpaca authority cutover.

This module owns the runner persistence semantics.  Cutover callers receive a
typed immutable snapshot and never parse binding, desired-state, or lifecycle
files themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from app.engine.live.account_artifacts import (
    AccountArtifactError,
    safe_account_artifact_id,
)
from app.engine.live.account_registry import (
    ACTIVE_INSTANCE_BINDING_STATES,
    index_account_instance_bindings,
    read_account_instance_registry,
)
from app.engine.live.bot_lifecycle_state import (
    BotDutyOutcome,
    BotLifecyclePhase,
    BotLifecycleStateCorruptError,
    BotLifecycleStateRepo,
    stable_bot_lifecycle_state_path,
)
from app.engine.live.desired_state import (
    DesiredState,
    DesiredStateCorruptError,
    DesiredStateRepo,
    stable_desired_state_path,
)
from app.engine.live.identity import (
    strategy_instance_artifact_dir,
    validate_strategy_instance_id,
)
from app.services.bot_binding_repository import (
    BINDING_FILENAME,
    STRATEGY_INSTANCE_FILENAME,
    live_state_binding_repository,
)


class CutoverRosterEvidenceError(RuntimeError):
    """The durable runner roster cannot prove that every Alpaca bot is stopped."""


@dataclass(frozen=True)
class QuiescentAlpacaBot:
    """Canonical runner facts for one durably quiescent Alpaca bot."""

    strategy_instance_id: str
    artifact_dir: Path
    mode: Literal["log_only", "dry_run", "trade"]
    desired_state: DesiredState
    lifecycle_phase: BotLifecyclePhase
    terminal_outcome: BotDutyOutcome


def read_quiescent_alpaca_roster(
    artifacts_root: Path,
    account_id: str,
    *,
    legacy_strategy_instance_ids: frozenset[str],
) -> tuple[QuiescentAlpacaBot, ...]:
    """Read the account's registry/legacy roster with canonical repositories."""
    live_state_root = artifacts_root / "live_state"
    if (
        artifacts_root.is_symlink()
        or not artifacts_root.is_dir()
        or live_state_root.is_symlink()
        or not live_state_root.is_dir()
    ):
        raise CutoverRosterEvidenceError(
            "runner artifacts and live_state roots must be regular directories"
        )

    binding_repository = live_state_binding_repository(artifacts_root)
    try:
        safe_account_artifact_id(account_id)
    except AccountArtifactError as exc:
        if not legacy_strategy_instance_ids:
            raise CutoverRosterEvidenceError(
                f"account runner registry for {account_id!r} is invalid: {exc}"
            ) from exc
        account_bindings = index_account_instance_bindings(())
    else:
        try:
            account_bindings = index_account_instance_bindings(
                read_account_instance_registry(artifacts_root, account_id),
                account_id=account_id,
            )
        except (AccountArtifactError, OSError, ValidationError, ValueError) as exc:
            raise CutoverRosterEvidenceError(
                f"account runner registry for {account_id!r} is invalid: {exc}"
            ) from exc
    active_account_bindings = {
        strategy_instance_id: binding
        for strategy_instance_id, binding in account_bindings.latest_by_instance.items()
        if binding.lifecycle_state in ACTIVE_INSTANCE_BINDING_STATES
    }
    governed_ids = frozenset(active_account_bindings) | legacy_strategy_instance_ids
    if not governed_ids:
        raise CutoverRosterEvidenceError(
            f"runner evidence contains no Alpaca bots governed by account {account_id!r}"
        )
    roster: list[QuiescentAlpacaBot] = []
    for strategy_instance_id in sorted(governed_ids):
        account_binding = active_account_bindings.get(strategy_instance_id)
        instance_dir = strategy_instance_artifact_dir(
            artifacts_root,
            "live_state",
            strategy_instance_id,
        )
        if instance_dir.is_symlink():
            raise CutoverRosterEvidenceError(
                f"runner artifact {instance_dir.name!r} must not be a symbolic link"
            )
        if not instance_dir.is_dir():
            raise CutoverRosterEvidenceError(
                f"runner evidence for {strategy_instance_id!r} has no artifact directory"
            )
        try:
            strategy_instance_id = validate_strategy_instance_id(strategy_instance_id)
            if not _has_binding_candidate(instance_dir):
                raise ValueError(
                    "account registry binding has no runner binding evidence"
                )
            binding = binding_repository.read(strategy_instance_id)
            if binding is None:
                raise ValueError("binding has incomplete normalized run evidence")
            if account_binding is not None and binding.run_id != account_binding.run_id:
                raise ValueError(
                    "runner binding does not match the account registry run"
                )
            if binding.broker != "alpaca":
                continue
            desired_path = stable_desired_state_path(
                artifacts_root, strategy_instance_id
            )
            lifecycle_path = stable_bot_lifecycle_state_path(
                artifacts_root, strategy_instance_id
            )
            _require_regular_file(desired_path, "desired state")
            _require_regular_file(lifecycle_path, "lifecycle state")
            desired = DesiredStateRepo(
                desired_path,
                trusted_root=live_state_root,
            ).read()
            lifecycle = BotLifecycleStateRepo(lifecycle_path).read()
            if desired is None or lifecycle is None:
                raise ValueError("runner stop evidence is incomplete")
            _require_quiescent(
                strategy_instance_id=strategy_instance_id,
                desired_state=desired.desired_state,
                lifecycle_phase=lifecycle.phase,
                active_run_id=lifecycle.active_run_id,
                terminal_outcome=lifecycle.duty_outcome,
            )
        except (
            BotLifecycleStateCorruptError,
            CutoverRosterEvidenceError,
            DesiredStateCorruptError,
            OSError,
            ValueError,
        ) as exc:
            raise CutoverRosterEvidenceError(
                f"runner evidence for {instance_dir.name!r} is invalid: {exc}"
            ) from exc
        terminal_outcome = lifecycle.duty_outcome
        if terminal_outcome is None:  # pinned by _require_quiescent
            raise AssertionError("quiescent lifecycle lacks its terminal outcome")
        roster.append(
            QuiescentAlpacaBot(
                strategy_instance_id=strategy_instance_id,
                artifact_dir=instance_dir,
                mode=binding.mode,
                desired_state=desired.desired_state,
                lifecycle_phase=lifecycle.phase,
                terminal_outcome=terminal_outcome,
            )
        )
    if not roster:
        raise CutoverRosterEvidenceError(
            f"runner evidence contains no Alpaca bots governed by account {account_id!r}"
        )
    return tuple(roster)


def _has_binding_candidate(instance_dir: Path) -> bool:
    candidates = (
        instance_dir / STRATEGY_INSTANCE_FILENAME,
        instance_dir / BINDING_FILENAME,
    )
    for candidate in candidates:
        if candidate.is_symlink():
            raise CutoverRosterEvidenceError(
                f"binding candidate {candidate.name!r} must not be a symbolic link"
            )
        if candidate.exists() and not candidate.is_file():
            raise CutoverRosterEvidenceError(
                f"binding candidate {candidate.name!r} must be a regular file"
            )
    return any(candidate.is_file() for candidate in candidates)


def _require_regular_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise CutoverRosterEvidenceError(
            f"{label} must be a regular non-symbolic-link file"
        )


def _require_quiescent(
    *,
    strategy_instance_id: str,
    desired_state: DesiredState,
    lifecycle_phase: BotLifecyclePhase,
    active_run_id: str | None,
    terminal_outcome: BotDutyOutcome | None,
) -> None:
    if desired_state is not DesiredState.STOPPED:
        raise CutoverRosterEvidenceError(
            f"governed bot {strategy_instance_id!r} is not durably STOPPED"
        )
    if (
        lifecycle_phase
        not in {BotLifecyclePhase.OFF_DUTY, BotLifecyclePhase.RETIRED}
        or active_run_id is not None
    ):
        raise CutoverRosterEvidenceError(
            f"governed bot {strategy_instance_id!r} is not durably quiescent"
        )
    if terminal_outcome is None:
        raise CutoverRosterEvidenceError(
            f"governed bot {strategy_instance_id!r} lacks a durable terminal outcome"
        )
