"""Write sealed historical fixture bytes; no application arming writer survives."""
from __future__ import annotations

import json
from dataclasses import asdict

from app.broker.alpaca.clerk.live_arming import LedgerRecord, LiveArmingRecord, arming_version_payload
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger


class HistoricalArmingLedger(LiveArmingLedger):
    """Test fixture builder around the production read-only decoder."""

    def append(self, record: LedgerRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps((arming_version_payload(asdict(record), record.schema_version) if isinstance(record, LiveArmingRecord) else asdict(record)), sort_keys=True, separators=(",", ":")) + "\n")
