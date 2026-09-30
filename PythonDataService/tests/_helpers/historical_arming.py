"""Write sealed historical fixture bytes; no application arming writer survives."""
from __future__ import annotations

import json
from dataclasses import asdict

from app.broker.alpaca.clerk.live_arming import LedgerRecord, LiveArmingRecord, LiveDisarmRecord, arming_version_payload
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger


class HistoricalArmingLedger(LiveArmingLedger):
    """Test fixture builder around the production read-only decoder."""

    def append(self, record: LedgerRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps((arming_version_payload(asdict(record), record.schema_version) if isinstance(record, LiveArmingRecord) else asdict(record)), sort_keys=True, separators=(",", ":")) + "\n")


    def append_disarm_fixture(self, strategy_instance_id: str, *, disarmed_at_ms: int) -> None:
        old = self.records_for(strategy_instance_id)[-1]
        assert isinstance(old, LiveArmingRecord)
        self.append(LiveDisarmRecord.create(live_account_id=self.live_account_id,
            strategy_instance_id=strategy_instance_id, revokes_record_sha256=old.record_sha256,
            disarmed_at_ms=disarmed_at_ms))
