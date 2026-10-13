"""What a Deploy says its bot trades, and what the Deploy form is told about a bot that trades options (#2913).

**One choice.** A Deploy request says what its bot trades once, as one of
three: the shares of the symbol it listens to, another stock or ETF, or an
option structure. A stock and an option structure together cannot be said.

**An option choice** is the structure as the builder holds it: the
underlying, the name it was started from (none is Custom), its legs exactly
as the structure preview takes them, and the four order terms, which are
sealed with the bot and fixed for its life. Its size is the request's own
sizing, in whole structures.

**The review** is what the money review of an option Deploy carries beside
the budget: the money rows, what stops the Deploy and what the owner should
know, each a sentence the server wrote. A screen renders it and works
nothing out. Money is whole cents with the same amount written out in
dollars beside it. A leg is named by its place in the request, counted from
one.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.models import EpochMs
from app.broker.contract.models import US_EQUITY_SYMBOL_PATTERN
from app.options.named_structures import NamedStructure
from app.schemas.option_order_terms import SealedOptionOrderTerms
from app.schemas.option_preview import MAX_LEGS_ASKED, OptionContractView, OptionLegRules, PreviewState


class TradesTheSignal(BaseModel):
    """The shares of the symbol the bot listens to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["signal"]


class TradesAnotherStock(BaseModel):
    """Another stock or ETF than the symbol the bot listens to (#2912)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["stock"]
    symbol: str = Field(min_length=1, max_length=12)


class TradesOptions(BaseModel):
    """An option structure on one underlying, with the order terms it is deployed under."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["options"]
    # The stock or ETF the contracts are on. It may be the symbol the bot listens to.
    underlying: Annotated[str, Field(pattern=US_EQUITY_SYMBOL_PATTERN)]
    # The named structure the legs are still an example of. None: Custom.
    structure: NamedStructure | None = None
    legs: Annotated[list[OptionLegRules], Field(max_length=MAX_LEGS_ASKED)]
    order_terms: SealedOptionOrderTerms


DeployTrades = Annotated[TradesTheSignal | TradesAnotherStock | TradesOptions, Field(discriminator="kind")]


# ---- what the money review of an option Deploy carries ----------------------------------------------


class OptionDeployBlocker(BaseModel):
    """One thing that stops this Deploy, for the "Can't deploy yet" list."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # A closed code: the typed refusal it came from, or one of ``OPTION_DEPLOY_…``.
    gate_id: str
    # A few words: "Options approval".
    label: str
    headline: str
    # What to do about it, when something is known to help.
    fix: str | None


class OptionDeployNote(BaseModel):
    """Something the owner should read before deploying. Never a refusal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    headline: str
    detail: str


MoneyRowKey = Literal[
    "set_aside",
    "premium",
    "broker_requirement",
    "fees",
    "loss_at_expiry",
    "budget_free",
    "incremental_requirement",
    "headroom_left",
]


class OptionMoneyRow(BaseModel):
    """One row of the money an option entry would mean, as the structure preview priced it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: MoneyRowKey
    label: str
    # None when the figure is not estimated; ``note`` then says why.
    amount_cents: int | None
    # The same amount written out, to be shown as it is. A premium is written without a sign: ``note``
    # says which way it goes.
    amount_usd: str | None
    # "paid" or "received" beside the premium; the fees' own sentence; what an estimate assumes; or why
    # there is no figure.
    note: str | None
    # The premium, the requirement and the fees: the three rows that add up to what is set aside.
    part_of_set_aside: bool
    # A figure worked out from a reading is stale past that reading's age limit and unknown when it cannot be
    # worked out; ``note`` then says why, and ``as_of_ms`` when the reading was taken (#3099).
    state: Literal["known", "stale", "unknown"] = "known"
    as_of_ms: EpochMs | None = None


class OptionPickedContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # The leg's place in the request, counted from one.
    number: int
    # "Buy" or "Sell".
    side_label: str
    ratio: int
    contract: OptionContractView
    # The price a share of it is assumed at, in dollars: the ask for a leg that is bought and the bid for one
    # that is sold, as quoted when ``quotes_read_ms`` says.
    price_usd: str


# The structure preview's own states, and one more that is the review's: the preview was not asked.
ReviewPreviewState = PreviewState | Literal["not_asked"]


class OptionDeployReview(BaseModel):
    """What Deploy says of a bot that trades an option structure, beside its budget."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    underlying: str
    # "Bull call spread", and "a bull call spread on": for a list and for a sentence.
    structure_label: str
    structure_in_a_sentence: str
    structures: int
    # Confirm's Orders row: "Limit in, market out · gap up to 8% a leg · wait 20 s".
    orders_summary: str
    # What the two sizing choices mean for a bot that trades structures.
    one_structure_explanation: str
    custom_structures_explanation: str
    # Said under the bot's end when a sold leg makes the Clerk schedule a close every session. None otherwise.
    end_sentence: str | None
    # Where the structure preview this review read stood. Only ``resolved`` carries money rows.
    # ``not_asked``: the review read no preview, because something found first made it pointless (no plan
    # can hold the structure, or this Clerk has no option quotes of its own). It then carries no reason
    # code and no sentence of the preview's, and ``blockers`` says why.
    preview_state: ReviewPreviewState
    preview_reason_code: str | None
    preview_sentence: str | None
    # Set while the preview is only still arriving: ask the review again after this long.
    ask_again_in_ms: int | None
    quotes_read_ms: EpochMs | None
    # Past this the rows are shown with the time the quotes were read, or the review is asked again.
    figures_good_until_ms: EpochMs | None
    picks: tuple[OptionPickedContract, ...] = ()
    money_rows: tuple[OptionMoneyRow, ...] = ()
    # Said under the rows, for every option bot.
    money_sentence: str
    # Said when an entry made right now would be skipped though the structure can be deployed.
    entry_sentence: str | None
    # What the prices above are, when there are any.
    prices_sentence: str | None = None
    # How many whole entries of this size fit under the limit that binds, and what that count is not: every
    # bot competes for the same account headroom (#3099). ``None`` when no budget or account can say.
    fits: int | None = None
    fits_sentence: str | None = None
    blockers: tuple[OptionDeployBlocker, ...] = ()
    notes: tuple[OptionDeployNote, ...] = ()


__all__ = [
    "DeployTrades",
    "MoneyRowKey",
    "OptionDeployBlocker",
    "OptionDeployNote",
    "OptionDeployReview",
    "OptionMoneyRow",
    "OptionPickedContract",
    "ReviewPreviewState",
    "TradesAnotherStock",
    "TradesOptions",
    "TradesTheSignal",
]
