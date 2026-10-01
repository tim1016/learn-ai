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
    AccountArtifactError,
    account_artifacts_root,
)
from app.engine.live.account_binding_ledger import (
    account_binding_ledger_read_enabled,
    binding_ledger_parity,
    read_account_binding_commands,
)
from app.utils.session_anchors import MAX_TIMESTAMP_MS

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
    recorded_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    source: str = Field(min_length=1)


@dataclass(frozen=True)
class AccountInstanceBindingIndex:
    """Latest-row fold of account instance registry rows."""

    latest_by_instance: Mapping[str, AccountInstanceBinding]
    latest_by_namespace: Mapping[str, AccountInstanceBinding]
    active_by_namespace: Mapping[str, tuple[AccountInstanceBinding, ...]]


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


__all__ = [
    "ACCOUNT_INSTANCE_REGISTRY_FILENAME",
    "ACTIVE_INSTANCE_BINDING_STATES",
    "AccountInstanceBinding",
    "AccountInstanceBindingIndex",
    "bot_order_namespace_for_instance",
    "index_account_instance_bindings",
    "read_account_instance_registry",
]
