"""Read-only historical arming ledger; no active API or CLI writes this file.

The original account-rooted JSONL bytes and versioned seals remain evidence.
Only an explicit budget-authority cutover changes authorization; this reader
cannot mint a grant, renew it, revoke it or translate one into a bot budget.
Its readers are the one-time exit-terms upgrade
(``sqlite/runtime.py::SqliteAlpacaClerkFacade.upgrade_legacy_exit_terms``) and
the entry-allowance resolver (``program_leg._sealed_allowances``); nothing
judges permission against it (#2629).
"""

from __future__ import annotations

from pathlib import Path

from app.broker.alpaca.clerk.account_authority import require_real_account_id
from app.broker.alpaca.clerk.live_arming import (
    LedgerRecord,
    LiveArmingInvalid,
    LiveArmingRecord,
    LiveDisarmRecord,
)
from app.broker.alpaca.clerk.sealed_ledger import (
    read_canonical_jsonl_objects,
)
from app.broker.alpaca.paths import resolve_contained_path, safe_path_component

LIVE_ARMING_FILENAME = "live_arming.jsonl"
_LABEL = "live arming"
_ARMING_DIR = "arming"


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

    def records_for(self, strategy_instance_id: str) -> tuple[LedgerRecord, ...]:
        return tuple(row for row in self.records() if row.strategy_instance_id == strategy_instance_id)


__all__ = ["LIVE_ARMING_FILENAME", "LiveArmingLedger"]
