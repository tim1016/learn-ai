"""Proof seam for the surviving read-only fold of #1019's legacy sidecar cure.

LegacyStaleClaimRetirementService (the operator-driven proof-and-retire
ceremony this file used to test end to end) retired along with the rest of
IBKR account authority (PR-A of #1813, fix round 2, 2026-08-27) — its HTTP
surface and both frontend callers were deleted by the same PR, leaving it
with zero production callers. What survives is retired_legacy_claim_keys(),
still called by app.services.fleet_contamination.py, and that is all this
file tests now.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.engine.live.account_artifacts import ACCOUNT_EVENTS_FILENAME
from app.services.legacy_stale_claim_retirement import (
    LEGACY_STALE_CLAIM_RETIRED_EVENT,
    retired_legacy_claim_keys,
)

_ACCOUNT_ID = "DUM284968"
_RUN_ID = "legacy-run"
_SID = "legacy-spy"
_NAMESPACE = "learn-ai/legacy-spy/v1"


def test_legacy_retirement_receipts_are_snapshotted_once(tmp_path: Path) -> None:
    legacy_path = tmp_path / "accounts" / _ACCOUNT_ID / ACCOUNT_EVENTS_FILENAME
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            {
                "event_type": LEGACY_STALE_CLAIM_RETIRED_EVENT,
                "strategy_instance_id": _SID,
                "run_id": _RUN_ID,
                "symbol": "spy",
                "bot_order_namespace": _NAMESPACE,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    first = retired_legacy_claim_keys(tmp_path, _ACCOUNT_ID)
    legacy_path.write_text("{truncated", encoding="utf-8")
    second = retired_legacy_claim_keys(tmp_path, _ACCOUNT_ID)

    assert first == {(_SID, _RUN_ID, "SPY", _NAMESPACE)}
    assert second == first
