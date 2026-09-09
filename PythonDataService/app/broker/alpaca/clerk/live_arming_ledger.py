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

import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.account_authority import require_real_account_id
from app.broker.alpaca.clerk.live_arming import (
    LIVE_ARMING_INSTANCE_UNSEALED,
    LIVE_ARMING_NOT_ARMED,
    LedgerRecord,
    LiveArmingInvalid,
    LiveArmingRecord,
    LiveArmingRefused,
    LiveDisarmRecord,
)
from app.broker.alpaca.clerk.sealed_ledger import (
    append_canonical_jsonl_line,
    read_canonical_jsonl_objects,
)
from app.broker.alpaca.paths import resolve_contained_path, safe_path_component
from app.utils.advisory_lock import advisory_file_lock

logger = logging.getLogger(__name__)

LIVE_ARMING_FILENAME = "live_arming.jsonl"
_LABEL = "live arming"
_ARMING_DIR = "arming"


def _arming_root(artifacts_root: Path) -> Path:
    """The account-rooted arming tree; it need not exist yet."""
    return resolve_contained_path(artifacts_root, "accounts", _ARMING_DIR)


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
        with advisory_file_lock(self._path):
            self._append_locked(record)

    def _append_locked(self, record: LedgerRecord) -> None:
        """The append itself; the caller already holds this ledger's advisory lock.

        Factored out so a transaction that must read *and* write under one
        acquisition (``revoke_latest``) can reuse the write without taking the
        lock a second time, which would deadlock on the blocking acquire.
        """
        if record.live_account_id != self._live_account_id:
            raise LiveArmingInvalid(f"{_LABEL} record belongs to another live account than this ledger's")
        canonical = _verified_payload(record)
        append_canonical_jsonl_line(self._path, canonical, invalid=LiveArmingInvalid, label=_LABEL)

    def revoke_latest(self, strategy_instance_id: str, *, disarmed_at_ms: int) -> LiveDisarmRecord:
        """Revoke this instance's latest arming, reading and appending under one lock.

        Deciding *what* is being revoked and writing the row that revokes it
        are one transaction. Split across two acquisitions, a concurrent re-arm
        lands between them and ``revokes_record_sha256`` names a record that is
        no longer the latest, while two concurrent disarms both succeed. Status
        reads any trailing disarm row as revoking the instance either way, so
        the effective state and the audit evidence would disagree about a
        real-money permission -- the one place they must not.

        The refusal is the ceremony's own ``LIVE_ARMING_NOT_ARMED``: there is
        nothing to revoke when the instance's last row is a revocation, or when
        it has no row at all.

        A ledger file that does not exist yet is refused before the lock is
        taken: ``advisory_file_lock`` would ``mkdir`` the account directory and
        create the sibling lock file as a side effect of merely checking, and a
        ledger that does not exist cannot hold an arming row to revoke. The
        under-lock check below still runs for the existing-file case, so the
        race protection against a concurrent re-arm or double-disarm is
        unchanged.
        """
        if not self._path.exists():
            raise LiveArmingRefused(
                LIVE_ARMING_NOT_ARMED,
                f"{strategy_instance_id} has no arming record to revoke on {self._live_account_id}",
            )
        with advisory_file_lock(self._path):
            latest = self.latest(strategy_instance_id)
            if not isinstance(latest, LiveArmingRecord):
                raise LiveArmingRefused(
                    LIVE_ARMING_NOT_ARMED,
                    f"{strategy_instance_id} has no arming record to revoke on {self._live_account_id}",
                )
            record = LiveDisarmRecord.create(
                live_account_id=self._live_account_id,
                strategy_instance_id=strategy_instance_id,
                revokes_record_sha256=latest.record_sha256,
                disarmed_at_ms=disarmed_at_ms,
            )
            self._append_locked(record)
        return record

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

    @classmethod
    def discover(cls, artifacts_root: Path, *, strategy_instance_id: str) -> LiveArmingLedger | None:
        """The one account whose arming ledger names this instance, from the tree alone.

        ``disarm`` is the closed direction, and the shadow activation proof it
        would normally read can be deleted or damaged after an arming --
        precisely during the incident a revocation exists for. The arming rows
        already name their own live account, so the tree can answer the
        question the fence usually answers.

        A directory whose name is not a real, path-safe account id is not an
        arming ledger and is skipped; a ledger that will not verify is skipped
        too, but never quietly -- it is logged at error level with its
        traceback, because a damaged sibling must be visible and must not hide
        a readable one. ``None`` means no ledger names the instance. Two
        ledgers naming it is a refusal: nothing here can choose between two
        accounts that both armed the same instance id.
        """
        found: list[LiveArmingLedger] = []
        for directory in sorted(_arming_root(artifacts_root).glob("*")):
            if not (directory / LIVE_ARMING_FILENAME).is_file():
                continue
            try:
                ledger = cls(artifacts_root, live_account_id=require_real_account_id(directory.name))
            except ValueError:
                logger.error(
                    "an arming tree directory does not name a real live account; it is skipped",
                    extra={"action": "live_arming_ledger_invalid", "account_id": directory.name},
                    exc_info=True,
                )
                continue
            try:
                names_instance = bool(ledger.records_for(strategy_instance_id))
            except LiveArmingInvalid:
                logger.error(
                    "an arming ledger will not verify; it cannot answer for this instance",
                    extra={
                        "action": "live_arming_ledger_invalid",
                        "account_id": ledger.live_account_id,
                        "strategy_instance_id": strategy_instance_id,
                    },
                    exc_info=True,
                )
                continue
            if names_instance:
                found.append(ledger)
        if len(found) > 1:
            raise LiveArmingRefused(
                LIVE_ARMING_INSTANCE_UNSEALED,
                f"{strategy_instance_id} is armed on more than one live account "
                f"({', '.join(ledger.live_account_id for ledger in found)}); "
                "disarm cannot choose between them",
            )
        return found[0] if found else None

    def records_for(self, strategy_instance_id: str) -> tuple[LedgerRecord, ...]:
        return tuple(row for row in self.records() if row.strategy_instance_id == strategy_instance_id)

    def latest(self, strategy_instance_id: str) -> LedgerRecord | None:
        rows = self.records_for(strategy_instance_id)
        return rows[-1] if rows else None


__all__ = ["LIVE_ARMING_FILENAME", "LiveArmingLedger"]
