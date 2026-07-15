"""Account-scoped instance registry for live-paper bots."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.engine.live.account_artifacts import (
    ACCOUNT_RECOVERY_EVIDENCE_EVENT_TYPES,
    AccountArtifactError,
    _append_account_event,
    _safe_account_path_segment,
    account_artifacts_root,
    read_account_events,
)
from app.engine.live.exit_taxonomy import (
    CRASH_RETIRED_BINDING_SOURCES,
    LIVENESS_UNPROVEN_REGISTRY_SOURCE,
    RECOVERY_REQUIRED_RETIRED_BINDING_SOURCES,
    false_crash_repair_source,
    read_run_exit_evidence,
)
from app.engine.live.live_state_sidecar import _file_lock, _fsync_parent_dir
from app.schemas.live_runs import GateResult

ACCOUNT_INSTANCE_REGISTRY_FILENAME = "instance_registry.jsonl"
ACTIVE_INSTANCE_BINDING_STATES = frozenset({"DEPLOYED", "ACTIVE"})


class AccountInstanceBinding(BaseModel):
    """Append-only account registry binding for one runner identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    strategy_instance_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    bot_order_namespace: str = Field(min_length=1, max_length=256)
    cohort_id: str | None = Field(default=None, min_length=1, max_length=128)
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


@dataclass(frozen=True)
class AccountRegistryFalseCrashBackfillResult:
    """Summary of the append-only false-crash registry repair."""

    accounts_scanned: int
    candidate_rows: int
    rows_repaired: int
    rows_skipped_no_disproof: int
    invalid_account_dirs: int
    repaired_run_ids: tuple[str, ...]


@dataclass(frozen=True)
class AccountRegistryBootReconcileResult:
    """Durable retirements performed before a new daemon trusts siblings."""

    accounts_scanned: int
    active_bindings_found: int
    bindings_retired: int
    preserved_managed_run_ids: tuple[str, ...]


def bot_order_namespace_for_instance(strategy_instance_id: str) -> str:
    return f"learn-ai/{strategy_instance_id}/v1"


def write_account_instance_binding(
    artifacts_root: Path,
    binding: AccountInstanceBinding,
) -> Path:
    safe_account_id = _safe_account_path_segment(binding.account_id)
    accounts_root = os.path.realpath(os.path.join(os.fspath(artifacts_root), "accounts"))
    root_real = os.path.realpath(os.path.join(accounts_root, safe_account_id))
    registry_filename = os.path.basename(ACCOUNT_INSTANCE_REGISTRY_FILENAME)
    registry_real = os.path.realpath(os.path.join(root_real, registry_filename))
    root_prefix = root_real if root_real.endswith(os.sep) else f"{root_real}{os.sep}"
    if not registry_real.startswith(root_prefix):
        raise AccountArtifactError(f"registry path traversal detected for account_id: {binding.account_id!r}")
    path = Path(registry_real)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = binding.model_dump_json() + "\n"
    with _file_lock(path):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_parent_dir(path)
    _append_account_event(
        artifacts_root,
        binding.account_id,
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
    """Return the unsafe terminal binding that blocks restart, if any."""

    bindings = read_account_instance_registry(artifacts_root, account_id)
    events = read_account_events(artifacts_root, account_id)
    latest = latest_account_instance_binding(
        bindings,
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
    )
    if latest is None:
        return None
    if (
        latest.lifecycle_state != "RETIRED"
        or latest.source not in RECOVERY_REQUIRED_RETIRED_BINDING_SOURCES
    ):
        return None
    if has_account_recovery_evidence_after(events, latest.recorded_at_ms):
        return None
    return latest


def retire_unmanaged_active_bindings_on_daemon_boot(
    artifacts_root: Path,
    *,
    managed_run_ids: Set[str],
    now_ms: int,
) -> AccountRegistryBootReconcileResult:
    """Retire prior ``ACTIVE`` rows not owned by a live managed process.

    A newly booted daemon does not adopt children from an earlier boot. Its
    in-memory process registry is the liveness authority; a runtime sidecar is
    useful diagnostic evidence but cannot prove process identity. Retiring an
    unmanaged binding before the daemon serves requests removes its namespace
    from sibling trust and makes the bot's submit gate fail closed.
    """

    accounts_root = Path(artifacts_root) / "accounts"
    if not accounts_root.exists():
        return AccountRegistryBootReconcileResult(0, 0, 0, ())

    accounts_scanned = 0
    active_bindings_found = 0
    bindings_retired = 0
    preserved: list[str] = []
    next_recorded_at_ms = now_ms

    for account_dir in sorted(path for path in accounts_root.iterdir() if path.is_dir()):
        account_id = account_dir.name
        bindings = read_account_instance_registry(artifacts_root, account_id)
        accounts_scanned += 1
        latest_bindings = index_account_instance_bindings(
            bindings,
            account_id=account_id,
        ).latest_by_instance.values()
        for binding in latest_bindings:
            if binding.lifecycle_state != "ACTIVE":
                continue
            active_bindings_found += 1
            if binding.run_id in managed_run_ids:
                preserved.append(binding.run_id)
                continue
            next_recorded_at_ms = max(
                next_recorded_at_ms,
                binding.recorded_at_ms + 1,
            )
            write_account_instance_binding(
                artifacts_root,
                binding.model_copy(
                    update={
                        "lifecycle_state": "RETIRED",
                        "recorded_at_ms": next_recorded_at_ms,
                        "source": LIVENESS_UNPROVEN_REGISTRY_SOURCE,
                    }
                ),
            )
            bindings_retired += 1
            next_recorded_at_ms += 1

    return AccountRegistryBootReconcileResult(
        accounts_scanned=accounts_scanned,
        active_bindings_found=active_bindings_found,
        bindings_retired=bindings_retired,
        preserved_managed_run_ids=tuple(sorted(preserved)),
    )


def backfill_false_crash_registry_rows(
    artifacts_root: Path,
    *,
    account_id: str | None = None,
    now_ms: int | None = None,
) -> AccountRegistryFalseCrashBackfillResult:
    """Append corrected registry rows for latest crash labels disproven by status.

    Historical registry rows are append-only. The repair therefore writes a
    later ``RETIRED`` row with the corrected source only when the latest row for
    an instance is still ``host_daemon.process_crashed`` and that run's own
    ``run_status.json`` carries a non-crash exit reason. Rows without status
    evidence are left untouched.
    """

    accounts = _account_ids_for_false_crash_backfill(artifacts_root, account_id=account_id)
    accounts_scanned = 0
    candidate_rows = 0
    rows_repaired = 0
    rows_skipped_no_disproof = 0
    invalid_account_dirs = 0
    repaired_run_ids: list[str] = []
    next_recorded_at_ms = now_ms if now_ms is not None else int(time.time() * 1000)

    for account in accounts:
        try:
            bindings = read_account_instance_registry(artifacts_root, account)
        except AccountArtifactError:
            invalid_account_dirs += 1
            continue
        accounts_scanned += 1
        binding_index = index_account_instance_bindings(bindings, account_id=account)
        for latest in binding_index.latest_by_instance.values():
            if latest.lifecycle_state != "RETIRED" or latest.source not in CRASH_RETIRED_BINDING_SOURCES:
                continue
            candidate_rows += 1
            evidence = read_run_exit_evidence(artifacts_root / "live_runs" / latest.run_id)
            repaired_source = false_crash_repair_source(evidence)
            if repaired_source is None:
                rows_skipped_no_disproof += 1
                continue
            next_recorded_at_ms = max(next_recorded_at_ms, latest.recorded_at_ms + 1)
            write_account_instance_binding(
                artifacts_root,
                latest.model_copy(
                    update={
                        "recorded_at_ms": next_recorded_at_ms,
                        "source": repaired_source,
                    }
                ),
            )
            rows_repaired += 1
            repaired_run_ids.append(latest.run_id)
            next_recorded_at_ms += 1

    return AccountRegistryFalseCrashBackfillResult(
        accounts_scanned=accounts_scanned,
        candidate_rows=candidate_rows,
        rows_repaired=rows_repaired,
        rows_skipped_no_disproof=rows_skipped_no_disproof,
        invalid_account_dirs=invalid_account_dirs,
        repaired_run_ids=tuple(repaired_run_ids),
    )


def _account_ids_for_false_crash_backfill(artifacts_root: Path, *, account_id: str | None) -> tuple[str, ...]:
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
    "AccountRegistryBootReconcileResult",
    "AccountRegistryFalseCrashBackfillResult",
    "backfill_false_crash_registry_rows",
    "bot_order_namespace_for_instance",
    "compute_reconcile_namespaces",
    "crash_retired_restart_blocking_binding",
    "evaluate_account_instance_binding",
    "has_account_recovery_evidence_after",
    "index_account_instance_bindings",
    "latest_account_instance_binding",
    "read_account_instance_registry",
    "retire_unmanaged_active_bindings_on_daemon_boot",
    "write_account_instance_binding",
]
