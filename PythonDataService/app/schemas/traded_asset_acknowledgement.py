"""The owner's written acceptance of an untested traded asset (#2912).

A strategy's evidence is about trading the symbol it listens to. A bot that
trades something else is untested trading it, and the owner accepts that in
writing before any mode that contacts a broker. The acceptance is its own
record: the evidence override cannot stand in for it, because Deploy refuses
that override for a strategy whose proof is already accepted.

It lives in its own module so the bot's seal can carry it without importing
the Deploy request schema. The warning the owner accepts is written here too,
once, so every screen that shows it shows the same sentences.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

UNTESTED_TRADED_ASSET_ACKNOWLEDGEMENT = "I_ACCEPT_UNTESTED_TRADED_ASSET_RISK"


class UntestedTradedAssetAcknowledgement(BaseModel):
    """A fixed acknowledgement plus the owner's own reason, kept with the bot for good."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    acknowledgement: Literal["I_ACCEPT_UNTESTED_TRADED_ASSET_RISK"]
    reason: str

    @field_validator("reason")
    @classmethod
    def _require_substantive_reason(cls, value: str) -> str:
        reason = value.strip()
        if len(reason) < 10:
            raise ValueError("the reason for accepting an untested traded asset must contain at least 10 characters")
        if len(reason) > 500:
            raise ValueError("the reason for accepting an untested traded asset must contain at most 500 characters")
        return reason


class UntestedTradedAssetWarning(BaseModel):
    """What the strategy's evidence does not cover, for a bot that trades another asset.

    It claims nothing about profit, and it says what nothing else on Deploy
    can: a ticker does not tell an inverse or leveraged fund from an ordinary
    one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    headline: str
    detail: str
    # The sentence the owner agrees to. ``None`` for a Dry Run, which shows
    # the warning and asks for nothing.
    acceptance_label: str | None = None

    @classmethod
    def for_symbols(
        cls,
        signal_symbol: str,
        traded_symbol: str,
        *,
        acceptance_required: bool,
    ) -> UntestedTradedAssetWarning:
        return cls(
            headline=f"Tested trading {signal_symbol}. Untested trading {traded_symbol}.",
            detail=(
                "This strategy's evidence comes from trading the symbol it listens to. "
                f"Its results buying {traded_symbol} on {signal_symbol}'s signal have not been tested. "
                "Nothing here says the trade is profitable. "
                "This system cannot tell an inverse or leveraged fund from its ticker."
            ),
            acceptance_label=(
                f"I accept that this strategy is untested trading {traded_symbol}."
                if acceptance_required
                else None
            ),
        )


def strip_absent_traded_asset_acknowledgement(payload: dict) -> dict:
    """Drop an absent acceptance from a payload about to be content-hashed or stored.

    Every seal and record minted before the field existed carries none.
    Writing the ``None`` would change every bot-configuration hash, and a
    record that refuses unknown fields could no longer be read by the code
    that wrote it. A recorded acceptance stays, under the hash. Mutates
    ``payload`` in place and returns it.
    """
    if payload.get("traded_asset_acknowledgement") is None:
        payload.pop("traded_asset_acknowledgement", None)
    return payload
