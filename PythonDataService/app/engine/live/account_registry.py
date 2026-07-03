"""Account-scoped instance registry for live-paper bots."""

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
    _append_account_event,
    account_artifacts_root,
    read_account_events,
)
from app.engine.live.live_state_sidecar import _file_lock, _fsync_parent_dir
from app.schemas.live_runs import GateResult

ACCOUNT_INSTANCE_REGISTRY_FILENAME = "instance_registry.jsonl"
ACTIVE_INSTANCE_BINDING_STATES = frozenset({"DEPLOYED", "ACTIVE"})
CRASH_RETIRED_BINDING_SOURCES = frozenset({"host_daemon.process_crashed"})


class AccountInstanceBinding(BaseModel):
    """Append-only account registry binding for one runner identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    strategy_instance_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    bot_order_namespace: str = Field(min_length=1, max_length=256)
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


def write_account_instance_binding(
    artifacts_root: Path,
    binding: AccountInstanceBinding,
) -> Path:
    root = account_artifacts_root(artifacts_root, binding.account_id)
    root.mkdir(parents=True, exist_ok=True)
    path = root / ACCOUNT_INSTANCE_REGISTRY_FILENAME
    line = binding.model_dump_json() + "\n"
    with _file_lock(path):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_parent_dir(path)
    _append_account_event(
        root,
        {
            "event_type": "account_instance_binding_recorded",
            "account_id": binding.account_id,
            "strategy_instance_id": binding.strategy_instance_id,
            "run_id": binding.run_id,
            "bot_order_namespace": binding.bot_order_namespace,
            "lifecycle_state": binding.lifecycle_state,
            "recorded_at_ms": binding.recorded_at_ms,
            "source": binding.source,
        },
    )
    return path


def read_account_instance_registry(
    artifacts_root: Path,
    account_id: str,
) -> list[AccountInstanceBinding]:
    root = os.path.realpath(os.fspath(account_artifacts_root(artifacts_root, account_id)))
    registry_filename = os.path.basename(ACCOUNT_INSTANCE_REGISTRY_FILENAME)
    if registry_filename != ACCOUNT_INSTANCE_REGISTRY_FILENAME:
        raise AccountArtifactError("invalid account instance registry filename")
    path = os.path.realpath(os.path.join(root, registry_filename))
    try:
        common = os.path.commonpath([path, root])
    except ValueError as exc:
        raise AccountArtifactError(
            f"account instance registry path {path} cannot share a root with {root}"
        ) from exc
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
    """Return the crash-retired binding that blocks restart, if any."""

    bindings = read_account_instance_registry(artifacts_root, account_id)
    events = read_account_events(artifacts_root, account_id)
    latest = latest_account_instance_binding(
        bindings,
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
    )
    if latest is None:
        return None
    if latest.lifecycle_state != "RETIRED" or latest.source not in CRASH_RETIRED_BINDING_SOURCES:
        return None
    if has_account_recovery_evidence_after(events, latest.recorded_at_ms):
        return None
    return latest


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
        namespace
        for namespace in binding_index.active_by_namespace
        if namespace != current_namespace
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
    "bot_order_namespace_for_instance",
    "compute_reconcile_namespaces",
    "crash_retired_restart_blocking_binding",
    "evaluate_account_instance_binding",
    "has_account_recovery_evidence_after",
    "index_account_instance_bindings",
    "latest_account_instance_binding",
    "read_account_instance_registry",
    "write_account_instance_binding",
]
