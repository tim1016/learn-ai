"""The shadow receipt: sealed, per-instance proof that the shadow gate passed (ADR 0059 D2).

Append-only under ``accounts/shadow/``; every row is sha256-sealed over its
payload, so a receipt is either exactly what the gate wrote or invalid. Slice
6's arming ceremony reads ``ShadowReceiptStore.current`` and refuses to arm
without one (``LIVE_SHADOW_INCOMPLETE``). Dates are the sessions' calendar
opens in ``int64 ms UTC``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.account_authority import require_real_account_id
from app.broker.alpaca.clerk.synthetic_activation import canonical_sha256
from app.broker.alpaca.paths import resolve_contained_path
from app.utils.advisory_lock import advisory_file_lock

SHADOW_RECEIPTS_FILENAME = "shadow_receipts.jsonl"
_SHA256_LENGTH = 64


class ShadowReceiptInvalid(ValueError):
    """A shadow receipt row cannot be trusted."""


@dataclass(frozen=True)
class ShadowReceiptSession:
    session_open_ms: int
    shadow_run_id: str
    reconciliation_sha256: str


@dataclass(frozen=True)
class ShadowReceipt:
    schema_version: int
    live_account_id: str
    strategy_instance_id: str
    configured_signal_hash: str
    twin_account_id: str
    twin_strategy_instance_id: str
    required_sessions: int
    sessions: tuple[ShadowReceiptSession, ...]
    written_at_ms: int
    receipt_sha256: str

    @classmethod
    def create(
        cls,
        *,
        live_account_id: str,
        strategy_instance_id: str,
        configured_signal_hash: str,
        twin_account_id: str,
        twin_strategy_instance_id: str,
        required_sessions: int,
        sessions: Sequence[ShadowReceiptSession],
        written_at_ms: int,
    ) -> ShadowReceipt:
        unsigned = {
            "schema_version": 1,
            "live_account_id": require_real_account_id(live_account_id),
            "strategy_instance_id": strategy_instance_id,
            "configured_signal_hash": configured_signal_hash,
            "twin_account_id": require_real_account_id(twin_account_id),
            "twin_strategy_instance_id": twin_strategy_instance_id,
            "required_sessions": required_sessions,
            "sessions": [asdict(session) for session in sessions],
            "written_at_ms": written_at_ms,
        }
        record = cls(
            **{**unsigned, "sessions": tuple(sessions)},
            receipt_sha256=canonical_sha256(unsigned),
        )
        _validate(record)
        return record

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ShadowReceipt:
        try:
            sessions = tuple(ShadowReceiptSession(**session) for session in payload["sessions"])
            record = cls(**{**payload, "sessions": sessions})
        except (KeyError, TypeError, ValueError) as exc:
            raise ShadowReceiptInvalid("shadow receipt has an invalid shape") from exc
        _validate(record)
        if record.receipt_sha256 != canonical_sha256(_unsigned(record)):
            raise ShadowReceiptInvalid("shadow receipt digest does not verify")
        return record


def _unsigned(record: ShadowReceipt) -> dict[str, Any]:
    payload = asdict(record)
    del payload["receipt_sha256"]
    return payload


def _validate(record: ShadowReceipt) -> None:
    try:
        require_real_account_id(record.live_account_id)
        require_real_account_id(record.twin_account_id)
    except ValueError as exc:
        raise ShadowReceiptInvalid("shadow receipt names a shadow: account or a sim: one as real") from exc
    if record.schema_version != 1 or record.required_sessions < 1 or record.written_at_ms < 0:
        raise ShadowReceiptInvalid("shadow receipt has invalid integer facts")
    if len(record.configured_signal_hash) != _SHA256_LENGTH or any(
        len(session.reconciliation_sha256) != _SHA256_LENGTH or session.session_open_ms < 0 or not session.shadow_run_id
        for session in record.sessions
    ):
        raise ShadowReceiptInvalid("shadow receipt has invalid session facts")


class ShadowReceiptStore:
    """Append-only receipt ledger at ``accounts/shadow/shadow_receipts.jsonl``."""

    def __init__(self, artifacts_root: Path) -> None:
        self._path = resolve_contained_path(artifacts_root, "accounts", "shadow", SHADOW_RECEIPTS_FILENAME)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, receipt: ShadowReceipt) -> None:
        canonical = ShadowReceipt.from_payload(asdict(receipt))
        with advisory_file_lock(self._path):
            if self._path.is_symlink() or (self._path.exists() and not self._path.is_file()):
                raise ShadowReceiptInvalid("shadow receipt ledger must be a regular file")
            self._path.parent.mkdir(parents=True, exist_ok=True)
            existed = self._path.exists()
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(canonical), sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if not existed:
                directory_fd = os.open(self._path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)

    def all_for(self, strategy_instance_id: str) -> tuple[ShadowReceipt, ...]:
        return tuple(r for r in self._read_all() if r.strategy_instance_id == strategy_instance_id)

    def latest(self, strategy_instance_id: str) -> ShadowReceipt | None:
        receipts = self.all_for(strategy_instance_id)
        return receipts[-1] if receipts else None

    def current(
        self, strategy_instance_id: str, *, configured_signal_hash: str, required_sessions: int
    ) -> ShadowReceipt | None:
        """The latest receipt that still proves the gate for this seal and count, or ``None``."""
        latest = self.latest(strategy_instance_id)
        if (
            latest is None
            or latest.configured_signal_hash != configured_signal_hash
            or len(latest.sessions) < required_sessions
        ):
            return None
        return latest

    def any_for_account(self, live_account_id: str) -> bool:
        return any(r.live_account_id == live_account_id for r in self._read_all())

    def _read_all(self) -> list[ShadowReceipt]:
        if self._path.is_symlink() or (self._path.exists() and not self._path.is_file()):
            raise ShadowReceiptInvalid("shadow receipt ledger must be a regular file")
        if not self._path.exists():
            return []
        try:
            return [
                ShadowReceipt.from_payload(json.loads(raw))
                for raw in self._path.read_text(encoding="utf-8").splitlines()
                if raw
            ]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ShadowReceiptInvalid("shadow receipt ledger cannot be read") from exc


__all__ = [
    "SHADOW_RECEIPTS_FILENAME",
    "ShadowReceipt",
    "ShadowReceiptInvalid",
    "ShadowReceiptSession",
    "ShadowReceiptStore",
]
