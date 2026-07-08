from __future__ import annotations

import json
from pathlib import Path

from app.operator.notices.schema import NOTICE_CODE_CONTRACTS, OperatorNoticeCode
from tests.operator._helpers import get_literal_args

SNAPSHOT_PATH = Path(__file__).resolve().parents[2] / "app" / "operator" / "notices" / "snapshot.json"


def test_operator_notice_code_snapshot_matches() -> None:
    """OperatorNoticeCode is mirrored to the snapshot. Updating one without
    the other indicates drift and would break frontend types.
    """
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    actual = list(get_literal_args(OperatorNoticeCode))
    assert snapshot["operator_notice_codes"] == actual, (
        "OperatorNoticeCode literal drifted from snapshot.\n"
        f"Snapshot ({len(snapshot['operator_notice_codes'])} codes): "
        f"{snapshot['operator_notice_codes']}\n"
        f"Actual ({len(actual)} codes): {actual}\n"
        "If this change is intentional, update "
        "PythonDataService/app/operator/notices/snapshot.json "
        "and the matching Frontend literal in "
        "Frontend/src/app/models/operator-notice.ts."
    )


def test_operator_notice_contract_snapshot_matches() -> None:
    """The actionability/remedy pin is part of the notice-code contract."""
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    actual = {
        code: contract.model_dump(mode="json")
        for code, contract in NOTICE_CODE_CONTRACTS.items()
    }
    assert snapshot["operator_notice_contracts"] == actual
