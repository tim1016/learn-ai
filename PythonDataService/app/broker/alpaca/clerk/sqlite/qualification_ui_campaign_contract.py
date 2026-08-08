"""Canonical cross-stack contract for the bounded UI correlation campaign."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_CONTRACT_FILENAME = "alpaca-clerk-ui-correlation-campaign.v4.json"


class BoundedUiCampaignContract(BaseModel):
    """Versioned command, fixture, and measurement bounds shared across stacks."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[4]
    campaign_id: str = Field(min_length=1, max_length=128)
    component_campaign_id: str = Field(min_length=1, max_length=128)
    command: tuple[str, ...] = Field(min_length=6, max_length=6)
    timeout_seconds: int = Field(ge=1, le=3_600)
    max_receipt_bytes: int = Field(ge=1_024, le=1_048_576)
    max_evidence_refs: int = Field(ge=1, le=128)
    page_load_count: int = Field(ge=2, le=32)
    browser_reload_count: int = Field(ge=1, le=31)
    revision_sequence: tuple[int, ...] = Field(min_length=2, max_length=128)
    bootstrap_revision: int = Field(ge=0)
    historical_snapshot_state: str = Field(min_length=1, max_length=128)
    presented_action_id: str = Field(min_length=1, max_length=128)
    evidence_click_target: str = Field(min_length=1, max_length=128)
    evidence_button_name: str = Field(min_length=1, max_length=256)
    hide_evidence_button_name: str = Field(min_length=1, max_length=256)
    station_button_name: str = Field(min_length=1, max_length=256)
    account_id: str = Field(min_length=1, max_length=64)
    strategy_instance_id: str = Field(min_length=1, max_length=256)
    transaction_ref: str = Field(min_length=1, max_length=256)
    symbol: str = Field(min_length=1, max_length=32)
    base_timestamp_ms: int = Field(ge=1_000_000_000_000)

    @model_validator(mode="after")
    def validate_campaign(self) -> BoundedUiCampaignContract:
        if self.browser_reload_count != self.page_load_count - 1:
            raise ValueError("browser reload count must equal page loads after the first")
        if tuple(sorted(self.revision_sequence)) != self.revision_sequence:
            raise ValueError("UI campaign revisions must be nondecreasing")
        if len(set(self.revision_sequence)) == len(self.revision_sequence):
            raise ValueError("UI campaign must exercise an unchanged SSE revision")
        if len(set(self.revision_sequence)) == 1:
            raise ValueError("UI campaign must exercise an advancing SSE revision")
        if self.bootstrap_revision >= min(self.revision_sequence):
            raise ValueError("UI campaign bootstrap revision must precede measured revisions")
        if self.command[-1] != "--reporter=json":
            raise ValueError("UI campaign must emit machine-readable Playwright output")
        return self

    @property
    def expected_observation_count(self) -> int:
        """Return the exact number of browser/revision observations required."""

        return self.page_load_count * len(self.revision_sequence)

    def expected_measurements(self) -> dict[str, object]:
        """Return the complete bounded campaign measurement contract."""

        observation_count = self.expected_observation_count
        return {
            "campaign_id": self.campaign_id,
            "page_load_count": self.page_load_count,
            "browser_reload_count": self.browser_reload_count,
            "interaction_count": observation_count,
            "evidence_request_count": observation_count,
            "evidence_response_proof_reference_count": observation_count,
            "native_event_source_connection_count": observation_count,
            "lifecycle_request_count": 0,
            "observed_revision_count": observation_count,
            "lifecycle_request_action_ids": (),
            "revision_sequence": self.revision_sequence,
            "historical_snapshot_state": self.historical_snapshot_state,
            "presented_action_id": self.presented_action_id,
            "correlation_record_count": observation_count,
        }


def _canonical_contract_path() -> Path:
    """Locate the repository contract from source and qualification containers."""

    for parent in Path(__file__).resolve().parents:
        candidate = parent / "contracts" / _CONTRACT_FILENAME
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"canonical UI campaign contract is unavailable: {_CONTRACT_FILENAME}")


def load_bounded_ui_campaign_contract(path: Path) -> BoundedUiCampaignContract:
    """Read and strictly validate one version-four campaign contract."""

    try:
        encoded = path.read_bytes()
    except OSError as exc:
        raise ValueError("UI campaign contract is unavailable") from exc
    try:
        payload = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError("UI campaign contract must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("UI campaign contract must be a JSON object")
    try:
        return BoundedUiCampaignContract.model_validate_json(encoded, strict=True)
    except ValidationError as exc:
        raise ValueError(f"UI campaign contract is invalid: {exc}") from exc


UI_CAMPAIGN_CONTRACT_PATH = _canonical_contract_path()
UI_CAMPAIGN_CONTRACT_SHA256 = hashlib.sha256(UI_CAMPAIGN_CONTRACT_PATH.read_bytes()).hexdigest()
BOUNDED_UI_CAMPAIGN = load_bounded_ui_campaign_contract(UI_CAMPAIGN_CONTRACT_PATH)

__all__ = [
    "BOUNDED_UI_CAMPAIGN",
    "UI_CAMPAIGN_CONTRACT_PATH",
    "UI_CAMPAIGN_CONTRACT_SHA256",
    "BoundedUiCampaignContract",
    "load_bounded_ui_campaign_contract",
]
