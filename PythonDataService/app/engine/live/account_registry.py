"""Read-compatible historical IBKR account-instance registry projections."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.engine.live.account_artifacts import (
    ACCOUNT_RECOVERY_EVIDENCE_EVENT_TYPES,
    AccountArtifactError,
    account_artifacts_root,
    read_or_migrate_account_recovery_clearance,
)
from app.engine.live.account_binding_ledger import (
    AccountBindingCommand,
    BindingLedgerParity,
    account_binding_ledger_read_enabled,
    binding_ledger_parity,
    pending_binding_retirement_proposals,
    read_account_binding_commands,
)
from app.engine.live.exit_taxonomy import (
    CRASH_RETIRED_BINDING_SOURCES,
    TERMINAL_RESTART_BLOCKING_BINDING_SOURCES,
)
from app.schemas.live_runs import GateResult

ACCOUNT_INSTANCE_REGISTRY_FILENAME = "instance_registry.jsonl"
ACTIVE_INSTANCE_BINDING_STATES = frozenset({"DEPLOYED", "ACTIVE"})


class AccountInstanceBinding(BaseModel):
    """One durable row authored by the retired IBKR runtime."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    strategy_instance_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    bot_order_namespace: str = Field(min_length=1, max_length=256)
    # Read-only compatibility for bindings written before cohort launches were
    # removed. The retained reader never serializes this field.
    cohort_id: str | None = Field(default=None, min_length=1, max_length=128, exclude=True)
    lifecycle_state: Literal["DEPLOYED", "ACTIVE", "RETIRED"] = "ACTIVE"
    recorded_at_ms: int = Field(ge=0)
    source: str = Field(min_length=1)


@dataclass(frozen=True)
class AccountInstanceBindingIndex:
    """Latest-row fold of account instance registry rows."""

    latest_by_instance: Mapping[str, AccountInstanceBinding]
    latest_by_namespace: Mapping[str, AccountInstanceBinding]
    active_by_namespace: Mapping[str, tuple[AccountInstanceBinding, ...]]

    @property
    def duplicate_active_namespaces(self) -> frozenset[str]:
        return frozenset(
            namespace
            for namespace, namespace_bindings in self.active_by_namespace.items()
            if len(namespace_bindings) > 1
        )


def bot_order_namespace_for_instance(strategy_instance_id: str) -> str:
    return f"learn-ai/{strategy_instance_id}/v1"


def read_account_instance_registry(
    artifacts_root: Path,
    account_id: str,
) -> list[AccountInstanceBinding]:
    """Read historical rows, optionally preferring a parity-clean ledger."""

    if account_binding_ledger_read_enabled():
        decisions = [
            AccountInstanceBinding.model_validate(
                command.model_dump(
                    mode="json",
                    exclude={"seq", "entry_kind", "proposal_seq"},
                )
            )
            for command in read_account_binding_commands(artifacts_root, account_id)
            if command.entry_kind == "decision"
        ]
        # Never make an accidental empty shadow ledger look like a clean empty
        # account. The flip is only safe after parity is clean; otherwise keep
        # the compatibility reader live rather than dropping a legacy-only bot.
        legacy_bindings = _read_legacy_account_instance_registry(artifacts_root, account_id)
        parity = binding_ledger_parity(
            artifacts_root,
            account_id=account_id,
            legacy_bindings=(binding.model_dump(mode="json") for binding in legacy_bindings),
        )
        if decisions and parity.is_clean:
            return decisions
        return legacy_bindings
    return _read_legacy_account_instance_registry(artifacts_root, account_id)


def _read_legacy_account_instance_registry(
    artifacts_root: Path,
    account_id: str,
) -> list[AccountInstanceBinding]:
    """Read the compatibility registry without applying the migration read flag."""

    root = os.path.realpath(os.fspath(account_artifacts_root(artifacts_root, account_id)))
    registry_filename = os.path.basename(ACCOUNT_INSTANCE_REGISTRY_FILENAME)
    if registry_filename != ACCOUNT_INSTANCE_REGISTRY_FILENAME:
        raise AccountArtifactError("invalid account instance registry filename")
    path = os.path.realpath(os.path.join(root, registry_filename))
    try:
        common = os.path.commonpath([path, root])
    except ValueError as exc:
        raise AccountArtifactError(f"account instance registry path {path} cannot share a root with {root}") from exc
    if common != root:
        raise AccountArtifactError(f"path traversal detected for account_id: {account_id!r}")
    root_prefix = root if root.endswith(os.sep) else f"{root}{os.sep}"
    if not path.startswith(root_prefix):
        raise AccountArtifactError(f"path traversal detected for account_id: {account_id!r}")
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return []
    except IsADirectoryError as exc:
        raise AccountArtifactError(f"account instance registry is not a file: {path}") from exc
    bindings: list[AccountInstanceBinding] = []
    for line in lines:
        if not line.strip():
            continue
        bindings.append(AccountInstanceBinding.model_validate_json(line))
    return bindings


def index_account_instance_bindings(
    bindings: Sequence[AccountInstanceBinding],
    *,
    account_id: str | None = None,
) -> AccountInstanceBindingIndex:
    """Fold registry rows into latest-row views.

    Newer ``recorded_at_ms`` wins. When two rows share the same timestamp, the
    later append wins because the account registry is append-only.
    """
    latest_by_instance: dict[str, AccountInstanceBinding] = {}
    latest_by_namespace: dict[str, AccountInstanceBinding] = {}
    for binding in bindings:
        if account_id is not None and binding.account_id.upper() != account_id.upper():
            continue
        latest_instance = latest_by_instance.get(binding.strategy_instance_id)
        if latest_instance is None or binding.recorded_at_ms >= latest_instance.recorded_at_ms:
            latest_by_instance[binding.strategy_instance_id] = binding

        latest_namespace = latest_by_namespace.get(binding.bot_order_namespace)
        if latest_namespace is None or binding.recorded_at_ms >= latest_namespace.recorded_at_ms:
            latest_by_namespace[binding.bot_order_namespace] = binding

    active_lists_by_namespace: dict[str, list[AccountInstanceBinding]] = {}
    for binding in latest_by_instance.values():
        if binding.lifecycle_state not in ACTIVE_INSTANCE_BINDING_STATES:
            continue
        active_lists_by_namespace.setdefault(binding.bot_order_namespace, []).append(binding)

    return AccountInstanceBindingIndex(
        latest_by_instance=MappingProxyType(latest_by_instance),
        latest_by_namespace=MappingProxyType(latest_by_namespace),
        active_by_namespace=MappingProxyType(
            {
                namespace: tuple(namespace_bindings)
                for namespace, namespace_bindings in active_lists_by_namespace.items()
            }
        ),
    )


def latest_account_instance_binding(
    bindings: list[AccountInstanceBinding],
    *,
    account_id: str,
    strategy_instance_id: str,
) -> AccountInstanceBinding | None:
    return index_account_instance_bindings(
        bindings,
        account_id=account_id,
    ).latest_by_instance.get(strategy_instance_id)


def has_account_recovery_evidence_after(events: list[dict], recorded_at_ms: int) -> bool:
    for event in events:
        if event.get("event_type") not in ACCOUNT_RECOVERY_EVIDENCE_EVENT_TYPES:
            continue
        try:
            event_ts_ms = int(event.get("ts_ms") or 0)
        except (TypeError, ValueError):
            continue
        if event_ts_ms > recorded_at_ms:
            return True
    return False


def crash_retired_restart_blocking_binding(
    artifacts_root: Path,
    *,
    account_id: str,
    strategy_instance_id: str,
) -> AccountInstanceBinding | None:
    """Return the newest unresolved terminal binding that blocks restart, if any.

    Covers every terminal source whose exposure is unproven at exit: a crash,
    a daemon-boot binding whose liveness is unproven, and an ``ended_without_status``
    exit (SIGKILL/OOM before a run-status receipt). Each requires an audited
    recovery override or a retire-and-replace before the same instance restarts.
    A later deploy-only binding stages a successor run, but does not resolve an
    earlier terminal outcome for the same immutable identity.
    """

    bindings = read_account_instance_registry(artifacts_root, account_id)
    if account_binding_ledger_read_enabled():
        # Parity deliberately compares each identity's latest state. That makes
        # the ledger flip safe for normal current-state consumers, but an older
        # persisted terminal retirement can be absent from a clean ledger replay
        # once a newer deploy-only decision exists. Restart safety needs that
        # complete history until historic registry rows have been retired.
        legacy_bindings = _read_legacy_account_instance_registry(artifacts_root, account_id)
        binding_keys = {
            (
                binding.account_id,
                binding.strategy_instance_id,
                binding.run_id,
                binding.bot_order_namespace,
                binding.lifecycle_state,
                binding.recorded_at_ms,
                binding.source,
            )
            for binding in bindings
        }
        bindings.extend(
            binding
            for binding in legacy_bindings
            if (
                binding.account_id,
                binding.strategy_instance_id,
                binding.run_id,
                binding.bot_order_namespace,
                binding.lifecycle_state,
                binding.recorded_at_ms,
                binding.source,
            )
            not in binding_keys
        )
    recovery_clearance = read_or_migrate_account_recovery_clearance(artifacts_root, account_id)
    latest_by_run: dict[str, AccountInstanceBinding] = {}
    for binding in bindings:
        if binding.account_id.upper() != account_id.upper() or binding.strategy_instance_id != strategy_instance_id:
            continue
        latest = latest_by_run.get(binding.run_id)
        if latest is None or binding.recorded_at_ms >= latest.recorded_at_ms:
            # Backfill corrections append a nonblocking RETIRED row for the
            # same run. A deploy-only successor has a different run ID and
            # must not hide a real terminal retirement from an earlier run.
            latest_by_run[binding.run_id] = binding

    newest_unresolved: AccountInstanceBinding | None = None
    for binding in latest_by_run.values():
        if binding.lifecycle_state != "RETIRED" or binding.source not in TERMINAL_RESTART_BLOCKING_BINDING_SOURCES:
            continue
        if recovery_clearance is not None and recovery_clearance.cleared_at_ms > binding.recorded_at_ms:
            continue
        if newest_unresolved is None or binding.recorded_at_ms >= newest_unresolved.recorded_at_ms:
            # The append-only ledger defines later append as the tiebreaker for
            # equal timestamps, matching ``index_account_instance_bindings``.
            newest_unresolved = binding
    return newest_unresolved


def account_recovery_evidence_exists_after(
    artifacts_root: Path,
    *,
    account_id: str,
    recorded_at_ms: int,
) -> bool:
    """Read recovery clearance from its typed recovery artifact.

    Operator history can describe a recovery, but it cannot clear a restart
    block. A valid clearance is the only durable state transition that may do
    so, and is retained in ``account_recovery_clearance.json`` even when no
    unresolved-exposure freeze existed.
    """

    evidence = read_or_migrate_account_recovery_clearance(artifacts_root, account_id)
    return evidence is not None and evidence.cleared_at_ms > recorded_at_ms


def pending_account_binding_retirements(
    artifacts_root: Path,
    *,
    account_id: str,
    strategy_instance_id: str | None = None,
) -> tuple[AccountBindingCommand, ...]:
    """Read historical retirement proposals that were never folded."""

    return pending_binding_retirement_proposals(
        artifacts_root,
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
    )


def account_binding_ledger_parity(
    artifacts_root: Path,
    *,
    account_id: str,
) -> BindingLedgerParity:
    """Compare the two retained historical ledgers without repairing either."""

    return binding_ledger_parity(
        artifacts_root,
        account_id=account_id,
        legacy_bindings=(
            binding.model_dump(mode="json")
            for binding in _read_legacy_account_instance_registry(artifacts_root, account_id)
        ),
    )


def _account_ids_for_registry_scan(artifacts_root: Path, *, account_id: str | None) -> tuple[str, ...]:
    if account_id is not None:
        return (account_id,)
    accounts_root = artifacts_root / "accounts"
    if not accounts_root.exists():
        return ()
    return tuple(sorted(path.name for path in accounts_root.iterdir() if path.is_dir()))


def compute_reconcile_namespaces(
    *,
    artifacts_root: Path,
    account_id: str,
    current_namespace: str,
) -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(owned_namespaces, known_sibling_namespaces)`` for reconciliation.

    Owned namespaces are adoptable into the current run's WAL. Sibling
    namespaces are recognized as same-account managed activity, but never
    adoptable by this run.
    """
    binding_index = index_account_instance_bindings(
        read_account_instance_registry(artifacts_root, account_id),
        account_id=account_id,
    )

    sibling_namespaces = {
        binding.bot_order_namespace
        for binding in binding_index.latest_by_instance.values()
        if binding.bot_order_namespace != current_namespace
    }
    return frozenset({current_namespace}), frozenset(sibling_namespaces)


def evaluate_account_instance_binding(
    artifacts_root: Path,
    *,
    account_id: str,
    strategy_instance_id: str,
    run_id: str,
    bot_order_namespace: str,
) -> GateResult:
    pending = pending_account_binding_retirements(
        artifacts_root,
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
    )
    if pending:
        latest_pending = pending[-1]
        return _registry_gate_result(
            status="block",
            reason="ACCOUNT_BINDING_RETIREMENT_PENDING",
            next_step="WAIT_FOR_ACCOUNT_CLERK_RECONCILIATION",
            evidence_at_ms=latest_pending.recorded_at_ms,
        )
    binding_index = index_account_instance_bindings(
        read_account_instance_registry(artifacts_root, account_id),
    )

    current = binding_index.latest_by_instance.get(strategy_instance_id)
    if current is None:
        return _registry_gate_result(
            status="block",
            reason="ACCOUNT_REGISTRY_UNKNOWN_INSTANCE",
            next_step="DEPLOY_OR_START_RUNNER",
            evidence_at_ms=None,
        )
    if current.lifecycle_state not in ACTIVE_INSTANCE_BINDING_STATES:
        return _registry_gate_result(
            status="block",
            reason="ACCOUNT_REGISTRY_INACTIVE_BINDING",
            next_step="DEPLOY_OR_START_RUNNER",
            evidence_at_ms=current.recorded_at_ms,
        )
    if current.account_id != account_id:
        return _registry_gate_result(
            status="block",
            reason="ACCOUNT_REGISTRY_ACCOUNT_MISMATCH",
            next_step="CHECK_ACCOUNT_REGISTRY",
            evidence_at_ms=current.recorded_at_ms,
        )
    if current.run_id != run_id:
        return _registry_gate_result(
            status="block",
            reason="ACCOUNT_REGISTRY_STALE_RUN",
            next_step="STOP_STALE_RUNNER",
            evidence_at_ms=current.recorded_at_ms,
        )
    if current.bot_order_namespace != bot_order_namespace:
        return _registry_gate_result(
            status="block",
            reason="ACCOUNT_REGISTRY_NAMESPACE_MISMATCH",
            next_step="CHECK_ACCOUNT_REGISTRY",
            evidence_at_ms=current.recorded_at_ms,
        )

    namespace_owners = binding_index.active_by_namespace.get(bot_order_namespace, ())
    if len(namespace_owners) > 1:
        return _registry_gate_result(
            status="block",
            reason="ACCOUNT_REGISTRY_DUPLICATE_NAMESPACE",
            next_step="CHECK_ACCOUNT_REGISTRY",
            evidence_at_ms=max(binding.recorded_at_ms for binding in namespace_owners),
        )

    return _registry_gate_result(
        status="pass",
        reason="ACCOUNT_REGISTRY_MATCH",
        next_step="GATE_PASSING",
        evidence_at_ms=current.recorded_at_ms,
    )


def _registry_gate_result(
    *,
    status: Literal["pass", "block"],
    reason: str | None,
    next_step: str,
    evidence_at_ms: int | None,
) -> GateResult:
    return GateResult(
        gate_id="account.instance_registry",
        status=status,
        source="account_instance_registry",
        operator_reason=reason,
        operator_next_step=next_step,
        evidence_at_ms=0 if evidence_at_ms is None else evidence_at_ms,
    )


__all__ = [
    "ACCOUNT_INSTANCE_REGISTRY_FILENAME",
    "ACTIVE_INSTANCE_BINDING_STATES",
    "CRASH_RETIRED_BINDING_SOURCES",
    "AccountInstanceBinding",
    "AccountInstanceBindingIndex",
    "BindingLedgerParity",
    "account_binding_ledger_parity",
    "account_recovery_evidence_exists_after",
    "bot_order_namespace_for_instance",
    "compute_reconcile_namespaces",
    "crash_retired_restart_blocking_binding",
    "evaluate_account_instance_binding",
    "has_account_recovery_evidence_after",
    "index_account_instance_bindings",
    "latest_account_instance_binding",
    "pending_account_binding_retirements",
    "read_account_instance_registry",
]
