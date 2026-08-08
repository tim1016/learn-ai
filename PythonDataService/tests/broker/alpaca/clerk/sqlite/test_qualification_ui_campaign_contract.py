"""Contract tests for the shared bounded UI campaign fixture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.qualification_ui_campaign_contract import (
    BOUNDED_UI_CAMPAIGN,
    UI_CAMPAIGN_CONTRACT_PATH,
    load_bounded_ui_campaign_contract,
)


def test_canonical_ui_campaign_contract_is_loaded_from_cross_stack_fixture() -> None:
    assert UI_CAMPAIGN_CONTRACT_PATH.name.endswith(".v4.json")
    assert BOUNDED_UI_CAMPAIGN.schema_version == 4
    assert BOUNDED_UI_CAMPAIGN.command[-1] == "--reporter=json"
    assert BOUNDED_UI_CAMPAIGN.expected_observation_count == 25
    assert BOUNDED_UI_CAMPAIGN.expected_measurements()["revision_sequence"] == (
        41,
        41,
        42,
        42,
        43,
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("browser_reload_count", 3, "browser reload count"),
        ("revision_sequence", [41, 42, 43], "unchanged SSE revision"),
        ("revision_sequence", [41, 41, 41], "advancing SSE revision"),
        ("revision_sequence", [42, 41, 43], "nondecreasing"),
        ("bootstrap_revision", 41, "bootstrap revision"),
        ("bootstrap_revision", 42, "bootstrap revision"),
        (
            "command",
            [
                "npx",
                "playwright",
                "test",
                "tests/e2e/alpaca-clerk-ui-correlation.spec.ts",
                "--reporter=json",
                "--project=chromium",
            ],
            "machine-readable",
        ),
    ],
)
def test_ui_campaign_contract_rejects_weakened_measurement_bounds(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    payload = json.loads(UI_CAMPAIGN_CONTRACT_PATH.read_text())
    payload[field] = value
    contract_path = tmp_path / "campaign.json"
    contract_path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match=message):
        load_bounded_ui_campaign_contract(contract_path)


def test_ui_campaign_contract_rejects_unexpected_fields(tmp_path: Path) -> None:
    payload = json.loads(UI_CAMPAIGN_CONTRACT_PATH.read_text())
    payload["unreviewed_override"] = True
    contract_path = tmp_path / "campaign.json"
    contract_path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="extra_forbidden"):
        load_bounded_ui_campaign_contract(contract_path)
