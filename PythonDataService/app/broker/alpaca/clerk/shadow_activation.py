"""The ``shadow:`` world's activation fence (ADR 0059 D2).

Modelled on the synthetic fence: a shadow authority has no custody until a
caller deliberately activates it, and the proof is an append-only,
sha256-sealed row under ``accounts/shadow/``. No startup path appends here;
``activate_shadow_clerk_authority`` (shadow_authority.py) is the one writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.broker.alpaca.clerk.account_authority import require_shadow_account_id
from app.broker.alpaca.clerk.synthetic_activation import (
    IsolatedActivationConflict,
    IsolatedActivationInvalid,
    IsolatedActivationRecord,
    IsolatedActivationStore,
)

SHADOW_ACTIVATION_FILENAME = "shadow_activation.jsonl"


class ShadowActivationInvalid(IsolatedActivationInvalid):
    """The durable shadow activation fence cannot be trusted."""


class ShadowActivationConflict(ShadowActivationInvalid, IsolatedActivationConflict):
    """Another writer activated the same shadow authority generation first."""


@dataclass(frozen=True)
class ShadowActivationRecord(IsolatedActivationRecord):
    """One append-only activation proof for a shadow Clerk account."""

    require_account_id = staticmethod(require_shadow_account_id)
    invalid_error = ShadowActivationInvalid
    label = "shadow activation"


class ShadowActivationStore(IsolatedActivationStore):
    """The ``shadow:`` fence at ``accounts/shadow/shadow_activation.jsonl``."""

    record_type = ShadowActivationRecord
    conflict_error = ShadowActivationConflict

    def __init__(self, artifacts_root: Path) -> None:
        super().__init__(artifacts_root, namespace_dir="shadow", filename=SHADOW_ACTIVATION_FILENAME)

    def latest(self, account_id: str) -> ShadowActivationRecord | None:
        record = super().latest(account_id)
        assert record is None or isinstance(record, ShadowActivationRecord)
        return record


__all__ = [
    "SHADOW_ACTIVATION_FILENAME",
    "ShadowActivationConflict",
    "ShadowActivationInvalid",
    "ShadowActivationRecord",
    "ShadowActivationStore",
]
