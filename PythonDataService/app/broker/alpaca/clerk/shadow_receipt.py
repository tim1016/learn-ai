"""The shadow receipt: sealed, per-instance proof that the shadow gate passed (ADR 0059 D2).

Append-only under ``accounts/shadow/``; every row is sha256-sealed over its
payload, so a receipt is either exactly what the gate wrote or invalid. Slice
6's arming ceremony reads ``ShadowReceiptStore.current`` and refuses to arm
without one (``LIVE_SHADOW_INCOMPLETE``). Dates are the sessions' calendar
opens in ``int64 ms UTC``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.account_authority import require_real_account_id
from app.broker.alpaca.clerk.sealed_ledger import (
    append_canonical_jsonl_line,
    canonical_sha256,
    read_canonical_jsonl_objects,
)
from app.broker.alpaca.paths import resolve_contained_path
from app.utils.advisory_lock import advisory_file_lock
from app.utils.session_anchors import MAX_TIMESTAMP_MS

SHADOW_RECEIPTS_FILENAME = "shadow_receipts.jsonl"
_SHA256_LENGTH = 64
_LABEL = "shadow receipt"


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
            # Inside the guard: ``_validate`` compares fields this row has not
            # been type-checked for, and a type-confused row must still leave
            # by this module's own error, not as a bare ``TypeError``.
            _validate(record)
            # Inside the guard for the same reason: the digest is taken over the
            # record's own fields, so a type-confused row must leave by this
            # module's error here too, not as a bare ``TypeError``.
            if record.receipt_sha256 != canonical_sha256(_unsigned(record)):
                raise ShadowReceiptInvalid("shadow receipt digest does not verify")
        except ShadowReceiptInvalid:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ShadowReceiptInvalid("shadow receipt has an invalid shape") from exc
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
    if (
        record.schema_version != 1
        or record.required_sessions < 1
        or not 0 <= record.written_at_ms <= MAX_TIMESTAMP_MS
    ):
        raise ShadowReceiptInvalid("shadow receipt has invalid integer facts")
    if len(record.configured_signal_hash) != _SHA256_LENGTH or any(
        len(session.reconciliation_sha256) != _SHA256_LENGTH
        or not 0 <= session.session_open_ms <= MAX_TIMESTAMP_MS
        or not session.shadow_run_id
        for session in record.sessions
    ):
        raise ShadowReceiptInvalid("shadow receipt has invalid session facts")
    # The gate's meaning is N *distinct* trading sessions (ADR 0059 D2), and the
    # receipt — not its writer — is what slice 6 trusts, so it proves its own count.
    if len({session.session_open_ms for session in record.sessions}) != len(record.sessions):
        raise ShadowReceiptInvalid("shadow receipt repeats a session")
    if len(record.sessions) < record.required_sessions:
        raise ShadowReceiptInvalid("shadow receipt lists fewer sessions than it requires")


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
            append_canonical_jsonl_line(self._path, asdict(canonical), invalid=ShadowReceiptInvalid, label=_LABEL)

    def all_for(self, strategy_instance_id: str) -> tuple[ShadowReceipt, ...]:
        return tuple(r for r in self._read_all() if r.strategy_instance_id == strategy_instance_id)

    def latest(self, strategy_instance_id: str) -> ShadowReceipt | None:
        """This instance's last appended receipt, in file order.

        File order needs no monotonicity guard like the activation fence's: a
        receipt carries no generation to replay, and ``current()`` re-checks the
        seal and the session count on every read, so a re-appended older row can
        only answer for a caller it still satisfies.
        """
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
        return [
            ShadowReceipt.from_payload(payload)
            for payload in read_canonical_jsonl_objects(self._path, invalid=ShadowReceiptInvalid, label=_LABEL)
        ]


__all__ = [
    "SHADOW_RECEIPTS_FILENAME",
    "ShadowReceipt",
    "ShadowReceiptInvalid",
    "ShadowReceiptSession",
    "ShadowReceiptStore",
]
