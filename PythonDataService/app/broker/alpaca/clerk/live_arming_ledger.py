"""The append-only arming ledger for one live account (ADR 0059 D3, design R3).

``accounts/arming/<live_account_id>/live_arming.jsonl`` under the Clerk
artifacts root: one canonical JSON object per line, each sha256-sealed by
``live_arming.py``, appended under the advisory file lock with the same
``sealed_ledger`` discipline the shadow receipts use. File order is time order.

The tree is deliberately its own -- not ``accounts/alpaca/<id>/`` and not inside
a custody namespace directory -- so no custody-detection path (cutover
initialization, the latent-database checks) can mistake an arming ledger for an
authority. It is a sibling of ``accounts/shadow/`` and ``accounts/synthetic/``,
which is the shape those paths already tolerate.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.account_authority import require_real_account_id
from app.broker.alpaca.clerk.live_arming import (
    LedgerRecord,
    LiveArmingInvalid,
    LiveArmingRecord,
    LiveDisarmRecord,
)
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.broker.alpaca.clerk.sealed_ledger import (
    append_canonical_jsonl_line,
    read_canonical_jsonl_objects,
)
from app.broker.alpaca.paths import resolve_contained_path, safe_path_component
from app.utils.advisory_lock import advisory_file_lock

LIVE_ARMING_FILENAME = "live_arming.jsonl"
_LABEL = "live arming"
_ARMING_DIR = "arming"


def _verified_payload(record: LedgerRecord) -> dict[str, Any]:
    """What a reader will accept, re-derived from what a writer is about to write.

    The shadow receipt store's rule: a malformed record is refused before it
    reaches the file, rather than poisoning every later read of the ledger.
    """
    payload = asdict(record)
    verified: LedgerRecord = (
        LiveArmingRecord.from_payload(payload)
        if isinstance(record, LiveArmingRecord)
        else LiveDisarmRecord.from_payload(payload)
    )
    return asdict(verified)


class LiveArmingLedger:
    """Every arming and disarm row for one live account, in file order."""

    def __init__(self, artifacts_root: Path, *, live_account_id: str) -> None:
        self._live_account_id = require_real_account_id(live_account_id)
        self._path = resolve_contained_path(
            artifacts_root,
            "accounts",
            _ARMING_DIR,
            safe_path_component(self._live_account_id, "live account id"),
            LIVE_ARMING_FILENAME,
        )

    @property
    def path(self) -> Path:
        return self._path

    @property
    def live_account_id(self) -> str:
        return self._live_account_id

    def append(self, record: LedgerRecord) -> None:
        """Durably append one sealed row, under the lock, after re-verifying it."""
        if record.live_account_id != self._live_account_id:
            raise LiveArmingInvalid(f"{_LABEL} record belongs to another live account than this ledger's")
        canonical = _verified_payload(record)
        with advisory_file_lock(self._path):
            append_canonical_jsonl_line(self._path, canonical, invalid=LiveArmingInvalid, label=_LABEL)

    def records(self) -> tuple[LedgerRecord, ...]:
        """Every row this ledger's account owns, in file order.

        A row naming another account is ignored, not raised on: the tree is
        account-rooted, so such a row can only have been planted by hand, and
        one planted row must not make this account unreadable. A row whose
        ``kind`` is neither of the two *is* fatal -- it is a shape nothing here
        wrote, and guessing at it would be inventing custody evidence.
        """
        rows: list[LedgerRecord] = []
        for payload in read_canonical_jsonl_objects(self._path, invalid=LiveArmingInvalid, label=_LABEL):
            kind = payload.get("kind")
            if kind == "armed":
                record: LedgerRecord = LiveArmingRecord.from_payload(payload)
            elif kind == "disarmed":
                record = LiveDisarmRecord.from_payload(payload)
            else:
                raise LiveArmingInvalid(f"{_LABEL} record has an unrecognised kind")
            if record.live_account_id == self._live_account_id:
                rows.append(record)
        return tuple(rows)

    def records_for(self, strategy_instance_id: str) -> tuple[LedgerRecord, ...]:
        return tuple(row for row in self.records() if row.strategy_instance_id == strategy_instance_id)

    def latest(self, strategy_instance_id: str) -> LedgerRecord | None:
        rows = self.records_for(strategy_instance_id)
        return rows[-1] if rows else None

    def latest_arming(self) -> LiveArmingRecord | None:
        """The account's newest arming record, ignoring revocations (R10).

        A disarm withdraws one instance's permission; it does not unseal the
        account's envelope, which stays whatever the last arming ceremony read
        out of the environment until another ceremony replaces it.
        """
        armings = [row for row in self.records() if isinstance(row, LiveArmingRecord)]
        return armings[-1] if armings else None

    def sealed_envelope(self) -> LiveEnvelopeValues | None:
        latest = self.latest_arming()
        return None if latest is None else latest.envelope

    def instance_ids(self) -> tuple[str, ...]:
        """Every instance with a row here, in first-appearance order."""
        return tuple(dict.fromkeys(row.strategy_instance_id for row in self.records()))


__all__ = ["LIVE_ARMING_FILENAME", "LiveArmingLedger"]
