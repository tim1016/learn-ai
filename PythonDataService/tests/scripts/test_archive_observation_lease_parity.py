"""Tests for the durable #1021 parity-evidence archival command."""

from __future__ import annotations

import json

from app.engine.live.account_artifacts import append_account_event
from scripts.archive_observation_lease_parity import main


def _comparison(recorded_at_ms: int) -> dict[str, object]:
    return {
        "event_type": "account_observation_lease_shadow_comparison",
        "recorded_at_ms": recorded_at_ms,
        "strategy_instance_id": "bot-a",
        "run_id": "run-a",
        "truth_gate_id": "account.account_truth",
        "truth_source": "account_truth_snapshot",
        "truth_status": "pass",
        "lease_gate_id": "account.observation_lease",
        "lease_source": "account_observation_lease",
        "lease_status": "pass",
    }


def test_archive_observation_lease_parity_writes_digest_pinned_ready_report(tmp_path) -> None:
    for timestamp_ms in (1_704_209_400_000, 1_704_295_800_000, 1_704_382_200_000):
        append_account_event(tmp_path, "DU123", _comparison(timestamp_ms))
    output = tmp_path / "evidence" / "parity.json"

    exit_code = main(
        [
            "--artifacts-root",
            str(tmp_path),
            "--account-id",
            "DU123",
            "--output",
            str(output),
            "--require-ready",
        ]
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert payload["cutover_ready"] is True
    assert payload["observed_session_dates"] == ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert len(payload["source"]["account_events_sha256"]) == 64


def test_archive_observation_lease_parity_returns_nonzero_for_incomplete_evidence(tmp_path) -> None:
    append_account_event(tmp_path, "DU123", _comparison(1_704_209_400_000))
    output = tmp_path / "parity.json"

    exit_code = main(
        [
            "--artifacts-root",
            str(tmp_path),
            "--account-id",
            "DU123",
            "--output",
            str(output),
            "--require-ready",
        ]
    )

    assert exit_code == 2
    assert json.loads(output.read_text(encoding="utf-8"))["cutover_ready"] is False
