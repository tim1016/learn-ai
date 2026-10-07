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

from app.schemas.action_plan import ActionPlan, trades_the_signals_shares


def substantive_reason(value: str, *, what: str) -> str:
    """An owner's own reason for a recorded acceptance: 10 to 500 characters once trimmed.

    The one rule for every acceptance that is kept with a bot. ``what``
    names the reason in the refusal.
    """
    reason = value.strip()
    if len(reason) < 10:
        raise ValueError(f"{what} must contain at least 10 characters")
    if len(reason) > 500:
        raise ValueError(f"{what} must contain at most 500 characters")
    return reason


class UntestedTradedAssetAcknowledgement(BaseModel):
    """A fixed acknowledgement plus the owner's own reason, kept with the bot for good."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    acknowledgement: Literal["I_ACCEPT_UNTESTED_TRADED_ASSET_RISK"]
    reason: str

    @field_validator("reason")
    @classmethod
    def _require_substantive_reason(cls, value: str) -> str:
        return substantive_reason(value, what="the reason for accepting an untested traded asset")


AcknowledgementFault = Literal["required", "not_applicable"]


def traded_asset_acknowledgement_fault(
    *,
    mode: Literal["log_only", "dry_run", "trade"],
    action_plan: ActionPlan,
    signal_symbol: str,
    acknowledgement: UntestedTradedAssetAcknowledgement | None,
) -> AcknowledgementFault | None:
    """What is wrong with starting this bot with this acknowledgement, or ``None`` (#2912).

    The one rule, asked wherever a bot can be started. A bot needs the
    acknowledgement when it trades its account's money (``trade``: Paper,
    Live Shadow and Live alike) in something other than the shares it
    listens to. No other bot may carry one: the record is kept for good, and
    it would say the owner accepted what nobody asked them to.

    Whether a bot trades something else is read from its plan, by the
    instrument's type and identity, never by comparing two tickers: options
    on the bot's own symbol are something else.
    """
    return acknowledgement_fault(
        mode=mode,
        trades_something_else=not trades_the_signals_shares(action_plan, signal_symbol),
        acknowledgement=acknowledgement,
    )


def acknowledgement_fault(
    *,
    mode: Literal["log_only", "dry_run", "trade"],
    trades_something_else: bool,
    acknowledgement: UntestedTradedAssetAcknowledgement | None,
) -> AcknowledgementFault | None:
    """The rule itself, for a caller that knows what the bot trades without its plan in hand.

    A Deploy of an option structure is one: it trades something else
    whatever its legs come to, so the rule is asked before the plan is
    built (#2913).
    """
    needs_one = mode == "trade" and trades_something_else
    if needs_one:
        return "required" if acknowledgement is None else None
    return None if acknowledgement is None else "not_applicable"


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

    @classmethod
    def for_options(
        cls,
        signal_symbol: str,
        underlying: str,
        *,
        structure_in_a_sentence: str | None = None,
        acceptance_required: bool,
    ) -> UntestedTradedAssetWarning:
        """The warning for a bot that trades options (#2913). Options on the symbol it listens to included.

        ``structure_in_a_sentence`` is the structure as a sentence names it
        ("a bull call spread on"), once one is built; before that the
        warning says "options on".
        """
        traded = f"{structure_in_a_sentence or 'options on'} {underlying}"
        return cls(
            headline="Options have no backtest here.",
            detail=(
                f"This strategy's evidence comes from trading {signal_symbol} shares. "
                f"Its results trading {traded} on {signal_symbol}'s signal have not been tested. "
                "Options also lose value with time and move with volatility, which a share signal does not look at. "
                "Nothing here says the trade is profitable."
            ),
            acceptance_label=(
                f"I accept that this strategy is untested trading options on {underlying}."
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
