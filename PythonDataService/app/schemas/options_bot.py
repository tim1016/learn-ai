"""The immutable golden setup selected by an Options bot (ADR 0079)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.options_backtest.setup import OptionsBacktestSetup

OPTIONS_BOT_KEY = "options_rules"


class OptionsBotConfiguration(BaseModel):
    """An exact approved setup, independent of a mutable saved setup name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    golden_setup_id: str = Field(min_length=1, max_length=128)
    setup_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rules_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    setup: OptionsBacktestSetup

    @model_validator(mode="after")
    def _the_snapshot_is_the_approved_setup(self) -> OptionsBotConfiguration:
        from app.schemas.signal_program_seal import semantic_payload_hash

        if semantic_payload_hash(self.setup.model_dump(mode="json")) != self.setup_sha256:
            raise ValueError("The Options bot setup does not match its approved setup identity.")
        return self


def strip_absent_options_bot(payload: dict) -> dict:
    """Keep every historical Signal bot's serialized identity unchanged."""
    if payload.get("options_bot") is None:
        payload.pop("options_bot", None)
    return payload
