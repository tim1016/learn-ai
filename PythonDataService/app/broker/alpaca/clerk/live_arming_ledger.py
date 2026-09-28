"""Read-only historical arming ledger; no active API or CLI writes this file.

The original account-rooted JSONL bytes and versioned seals remain evidence.
Only an explicit budget-authority cutover changes authorization; this reader
cannot mint a grant, renew it, revoke it or translate one into a bot budget.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.broker.alpaca.clerk.account_authority import require_real_account_id
from app.broker.alpaca.clerk.live_arming import (
    LIVE_ARMING_INSTANCE_UNSEALED,
    LedgerRecord,
    LiveArmingInvalid,
    LiveArmingRecord,
    LiveArmingRefused,
    LiveDisarmRecord,
)
from app.broker.alpaca.clerk.sealed_ledger import (
    read_canonical_jsonl_objects,
)
from app.broker.alpaca.paths import resolve_contained_path, safe_path_component

logger = logging.getLogger(__name__)

LIVE_ARMING_FILENAME = "live_arming.jsonl"
_LABEL = "live arming"
_ARMING_DIR = "arming"


def _arming_root(artifacts_root: Path) -> Path:
    """The account-rooted arming tree; it need not exist yet."""
    return resolve_contained_path(artifacts_root, "accounts", _ARMING_DIR)


class LiveArmingLedger:
    """Read-only historical arming and disarm rows for one real account."""

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

        ``disarm`` is the closed direction and ``status`` is read-only. The
        shadow activation proof or runner binding they would normally read can
        be deleted or damaged after an arming -- precisely during the incident
        those operations need to report. The arming rows already name their
        own live account, so the tree can answer the question.

        A directory whose name is not a real, path-safe account id is not an
        arming ledger and is skipped; a ledger that will not verify is skipped
        too, but never quietly -- it is logged at error level with its
        traceback, because a damaged sibling must be visible and must not hide
        a readable one. ``None`` means no ledger names the instance. Two
        ledgers naming it is a refusal: nothing here can choose between two
        accounts that both armed the same instance id.
        """
        found = [cls(artifacts_root, live_account_id=account_id)
                 for account_id, records in cls.discover_records(artifacts_root).items()
                 if any(row.strategy_instance_id == strategy_instance_id for row in records)]
        if len(found) > 1:
            raise LiveArmingRefused(
                LIVE_ARMING_INSTANCE_UNSEALED,
                f"{strategy_instance_id} is armed on more than one live account "
                f"({', '.join(ledger.live_account_id for ledger in found)}); "
                "the historical reader cannot choose between them",
            )
        return found[0] if found else None

    @classmethod
    def discover_records(cls, artifacts_root: Path) -> dict[str, tuple[LedgerRecord, ...]]:
        """Read each confined ledger once; report corrupt siblings without hiding healthy ones."""
        found: dict[str, tuple[LedgerRecord, ...]] = {}
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
                records = ledger.records()
            except LiveArmingInvalid:
                logger.error(
                    "an arming ledger will not verify; it cannot answer for this instance",
                    extra={
                        "action": "live_arming_ledger_invalid",
                        "account_id": ledger.live_account_id,
                    },
                    exc_info=True,
                )
                continue
            found[ledger.live_account_id] = records
        return found

    def records_for(self, strategy_instance_id: str) -> tuple[LedgerRecord, ...]:
        return tuple(row for row in self.records() if row.strategy_instance_id == strategy_instance_id)

    def latest(self, strategy_instance_id: str) -> LedgerRecord | None:
        rows = self.records_for(strategy_instance_id)
        return rows[-1] if rows else None


__all__ = ["LIVE_ARMING_FILENAME", "LiveArmingLedger"]
