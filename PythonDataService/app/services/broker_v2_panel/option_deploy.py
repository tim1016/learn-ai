"""Deploy for a bot that trades an option structure: its plan, what stops it, and its money review (#2913).

Deploy takes an option choice on its request (``app.schemas.option_deploy``)
and this module says three things of it. Wherever an authority already holds
the rule, that authority is asked. What is decided here and nowhere else is
named below.

**The plan.** The Action Plan a Deploy carries is the one the structure
preview builds from the same legs, and a structure the preview's own plan
checks refuse is refused here in the preview's own words
(``plan_or_refusal``, one function that reads no quote), so the builder's
verdict and Deploy's cannot differ.

**What stops it.** Each is a typed blocker with a sentence that names a leg
by its place:

- the structure is one the account Clerk does not take yet. The Clerk's own
  rule is asked (``option_plan_taken``), so the two cannot differ;
- the account's mode. A Dry Run, Live Shadow and Paper account are open.
  A Live account is refused while
  ``ALPACA_LIVE_OPTION_BOTS_ENABLED`` is off, which it is unless a
  deployment turns it on (ADR 0076 decision 24);
- this Clerk has no option quotes of its own, or counts no quote line free
  for the plan. The first is the runner's own start refusal. The second is
  stricter than the runner's: the runner refuses a window larger than the
  Clerk's whole budget, and Deploy also takes off what the option bots
  already running here can need. That sum is this module's;
- on a real account: the account is not approved for the structure, its
  option buying power cannot cover one entry, or Alpaca does not list the
  contract the preview picked. The broker's port is asked; a Dry Run asks
  nothing of a broker. The level a structure needs (2 to buy an option, 3
  for several legs) and the comparison of buying power with what one entry
  sets aside are this module's;
- what the structure preview finds right now: picks that are not the
  structure built, or money that cannot be set aside;
- at the click, no price: an option bot is deployed only while one entry of
  its structure can be priced.

A click and an admission read are refused for the first found, and ask
nobody further. The money review lists every one it finds: it goes on to
read the preview and to ask Alpaca after a structure, a mode or a quote-line
blocker. Two things bound that list. A review of an account whose mode is
not open asks Alpaca nothing. And what needs a price or a pick (buying
power, the listing, a red verdict) is found only once the preview has
resolved.

**The money.** One entry is priced by the structure preview, which reads the
entry's own figures function. The rows here are that answer's fields under
the names the owner knows them by: what is set aside, and the premium,
Alpaca's requirement and the fee estimate that add up to it; the loss at
expiry if held, an estimate; and what the budget has free after one entry,
which is the one figure worked out here (the budget less what is set
aside). With no resolved preview there is no price, and the review says why.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from app.broker.alpaca.clerk.option_effects import (
    OptionEffectRefusal,
    OptionPlanRefused,
    option_plan_taken,
)
from app.broker.alpaca.live_option_bots import SETTING as LIVE_SETTING
from app.broker.alpaca.live_option_bots import live_option_bots_enabled
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import US_EQUITY_SYMBOL_PATTERN
from app.broker.contract.options import BrokerOptionAccountSnapshot, option_contract_refusal
from app.broker.contract.registry import get_broker_registry
from app.options.contract import OptionContract
from app.options.deadlines import CloseTerms
from app.options.structure_catalogue import in_a_sentence, label, orders_in_a_line
from app.options.structure_check import StructureRefusal
from app.options.watch import LineClass, lines_a_plan_can_need
from app.schemas.action_plan import ActionPlan, OptionEntryLeg
from app.schemas.broker_bots import AlpacaPaperDeployRequest
from app.schemas.option_deploy import (
    OptionDeployBlocker,
    OptionDeployNote,
    OptionDeployReview,
    OptionMoneyRow,
    OptionPickedContract,
    TradesOptions,
)
from app.schemas.option_preview import OptionStructurePreview, OptionStructurePreviewRequest
from app.services.bot_option_run import OptionMarket, this_process_option_market
from app.services.broker_v2_panel import option_structure_panel
from app.services.broker_v2_panel.budget_deploy import OptionEntryPrice
from app.services.broker_v2_panel.panel_errors import PanelRunnerError
from app.services.option_preview_words import in_the_builders_words, plural, sentence, usd
from app.services.option_structure_preview import PlanRefusal, plan_or_refusal
from app.utils.timestamps import now_ms_utc

# ---- reason codes: what stops an option Deploy, where no typed refusal already says it -------------

LIVE_SWITCHED_OFF: Final = "OPTION_DEPLOY_LIVE_SWITCHED_OFF"
APPROVAL_TOO_LOW: Final = "OPTION_DEPLOY_APPROVAL_TOO_LOW"
APPROVAL_UNKNOWN: Final = "OPTION_DEPLOY_APPROVAL_UNKNOWN"
ACCOUNT_NOT_READ: Final = "OPTION_DEPLOY_ACCOUNT_NOT_READ"
BUYING_POWER_SHORT: Final = "OPTION_DEPLOY_BUYING_POWER_SHORT"
CONTRACT_NOT_READ: Final = "OPTION_DEPLOY_CONTRACT_REFERENCE_NOT_READ"
NOT_PRICED: Final = "OPTION_DEPLOY_NOT_PRICED"

# ---- note codes -----------------------------------------------------------------------------------

FEES_ARE_AN_ESTIMATE: Final = "OPTION_DEPLOY_FEES_ARE_AN_ESTIMATE"
STREAM_NOT_SEEN: Final = "OPTION_DEPLOY_TRADE_STREAM_NOT_SEEN"
BUYING_POWER_UNKNOWN: Final = "OPTION_DEPLOY_BUYING_POWER_UNKNOWN"
ENTRY_WOULD_BE_SKIPPED: Final = "OPTION_DEPLOY_ENTRY_WOULD_BE_SKIPPED"
LEANS_AGAINST_THE_SIGNAL: Final = "OPTION_DEPLOY_LEANS_AGAINST_THE_SIGNAL"
UNTESTED_AT_ALPACA: Final = "OPTION_DEPLOY_NOT_CHECKED_AT_ALPACA"

# The option approval level Alpaca asks for: 2 to buy calls and puts, 3 for spreads and other
# structures of several legs (its options trading page, as ADR 0076 decision 12 reads it).
LEVEL_TO_BUY_OPTIONS: Final = 2
LEVEL_FOR_SEVERAL_LEGS: Final = 3

# How long a Deploy waits, at the click, for a preview that is only still arriving.
PRICE_PATIENCE_MS = 6_000

MONEY_SENTENCE: Final = (
    "The budget decides whether an entry may start. It is not a cap on what you can spend or lose. "
    "A market exit can do worse than these figures."
)
LIVE_SWITCHED_OFF_SENTENCE: Final = (
    "Option bots are switched off on Live accounts. Nothing in this build has been checked against Alpaca "
    "with real money at stake."
)
LIVE_SWITCHED_OFF_FIX: Final = (
    "Run an option bot on Paper and check its first option orders at Alpaca. Then set "
    f"{LIVE_SETTING}=true for this Clerk and restart it."
)


# ---- the request as the preview takes it --------------------------------------------------------------


def _lease_key(account_id: str, choice: TradesOptions) -> str:
    """The name Deploy's own ask for quotes goes under: one for a structure on an account, so every
    review of it renews one hold on quote lines and none is left behind by a changed budget."""
    said = json.dumps(
        {"account": account_id, "underlying": choice.underlying, "legs": [leg.model_dump(mode="json") for leg in choice.legs]},
        sort_keys=True,
    )
    return "deploy-" + hashlib.sha256(said.encode("utf-8")).hexdigest()[:40]


def preview_request_for(account_id: str, request: AlpacaPaperDeployRequest) -> OptionStructurePreviewRequest:
    """The structure preview's request for what this Deploy would trade: its legs, its size, its four terms."""
    choice = _choice(request)
    terms = choice.order_terms
    return OptionStructurePreviewRequest(
        preview_key=_lease_key(account_id, choice),
        underlying=choice.underlying,
        # Only the warning about leaning against the signal reads it. A symbol the preview's own request
        # would refuse is left unsaid, never a malformed request of this module's making.
        listens_to=request.symbol if re.fullmatch(US_EQUITY_SYMBOL_PATTERN, request.symbol) else None,
        structure=choice.structure,
        legs=list(choice.legs),
        structures=request.sizing.quantity,
        gap_limit_percent=terms.gap_limit_percent,
        cost_limit_percent=terms.cost_limit_percent,
        entry_wait_seconds=terms.entry_wait_seconds,
        expiry_close_lead_minutes=terms.expiry_close_lead_minutes,
    )


def _choice(request: AlpacaPaperDeployRequest) -> TradesOptions:
    choice = request.options
    if choice is None:
        raise ValueError("this Deploy trades shares; it has no option structure")
    return choice


def _order(choice: TradesOptions) -> list[str]:
    """The legs' ids in the request's order: a leg's place in it is the number a sentence names it by."""
    return [leg.leg_id for leg in choice.legs]


def _refusal(reason_code: str, message: str, *, detail: str, next_action: str) -> PanelRunnerError:
    return PanelRunnerError(message, detail=detail, next_action=next_action, http_status=409, reason_code=reason_code)


def _refused(blocker: OptionDeployBlocker) -> PanelRunnerError:
    return _refusal(
        blocker.gate_id,
        blocker.headline,
        detail=f"{blocker.label} stops this Deploy. Nothing was deployed and nothing was sent.",
        next_action=blocker.fix,
    )


# ---- the plan ---------------------------------------------------------------------------------------


def plan_blocker(refused: PlanRefusal) -> OptionDeployBlocker:
    """The structure preview's own refusal of the structure, as a blocker: its code and its words.

    The preview's plan checks are one function (``plan_or_refusal``), which
    reads no quote. Deploy asks that function and nothing else of the
    preview to learn whether a plan stands, so it never reads an answer
    about quotes as an answer about the plan.
    """
    return OptionDeployBlocker(
        gate_id=str(refused.reason),
        label="Structure",
        headline=refused.why,
        fix="Change the structure in the builder, then review the Deploy again.",
    )


# ---- what stops it ------------------------------------------------------------------------------------


def structure_not_taken(request: AlpacaPaperDeployRequest, plan: ActionPlan) -> OptionDeployBlocker | None:
    """Why the account Clerk does not take this structure yet, or ``None`` when it does: the Clerk's own rule."""
    try:
        option_plan_taken(plan, real_account=request.execution_mode in ("paper", "live"))
    except OptionPlanRefused as refused:
        return OptionDeployBlocker(
            gate_id=refused.reason.value,
            label="Structure",
            headline=in_the_builders_words(refused.detail, _order(_choice(request))),
            fix=(
                "Deploy a single bought call or put. The builder and the preview still show this structure, "
                "and it can be deployed once structures of several legs are built."
            ),
        )
    return None


def mode_blocker(request: AlpacaPaperDeployRequest) -> OptionDeployBlocker | None:
    """Why this account's mode takes no option bot, or ``None``: a Dry Run, Live Shadow and Paper account do."""
    if request.execution_mode == "live" and not live_option_bots_enabled():
        return OptionDeployBlocker(
            gate_id=LIVE_SWITCHED_OFF, label="Live", headline=LIVE_SWITCHED_OFF_SENTENCE, fix=LIVE_SWITCHED_OFF_FIX
        )
    return None


def quote_line_blockers(plan: ActionPlan, market: OptionMarket) -> list[OptionDeployBlocker]:
    """Why this Clerk could not quote the plan's contracts.

    No option quotes of its own is the refusal its runner would start with.
    The count of lines is held more strictly here than there: the runner
    refuses a window larger than the Clerk's whole budget, and Deploy also
    takes off what the windows of the option bots already running on this
    Clerk can need. The count is this system's own estimate. It cannot see
    what another IBKR session uses.
    """
    if market.quotes_are_read_elsewhere():
        return [
            OptionDeployBlocker(
                gate_id=OptionEffectRefusal.NO_OPTION_QUOTES_HERE.value,
                label="Option quotes",
                headline=(
                    "This Clerk reads its market status from a shared source, so it has no IBKR option quotes of "
                    "its own. No contract could be picked or priced here."
                ),
                fix="Deploy this bot on a Clerk with its own IBKR connection.",
            )
        ]
    needed = lines_a_plan_can_need(plan, strikes_each_side=market.strikes_each_side)
    taken = sum(
        lines_a_plan_can_need(running.plan, strikes_each_side=market.strikes_each_side)
        for running in market.watch.requests()
        if running.line_class is LineClass.CANDIDATE and running.plan is not None
    )
    free = market.line_budget - taken
    if needed <= free:
        return []
    cannot_see = "Other IBKR sessions share the same allowance, so the count is its best estimate."
    if free <= 0:
        counted = "This system counts no IBKR quote lines free for another options bot."
    else:
        counted = (
            f"This system counts {plural(free, 'IBKR quote line')} free, and this structure can need {needed}."
        )
    return [
        OptionDeployBlocker(
            gate_id=OptionEffectRefusal.WINDOW_OVER_LINE_BUDGET.value,
            label="IBKR quote lines",
            headline=(
                f"{counted} Its budget of option quote lines is {market.line_budget}, and the option bots "
                f"running here can need {taken}. {cannot_see}"
            ),
            fix="Raise this Clerk's IBKR_OPTION_LINE_BUDGET, stop another options bot, or deploy a narrower structure.",
        )
    ]


def _level_needed(plan: ActionPlan) -> int:
    legs = [leg for leg in plan.on_enter if isinstance(leg, OptionEntryLeg)]
    bought_alone = len(legs) == 1 and legs[0].position == "long"
    return LEVEL_TO_BUY_OPTIONS if bought_alone else LEVEL_FOR_SEVERAL_LEGS


_LEVEL_IN_WORDS: Final = {0: "not approved for options", 1: "approved for covered calls and cash-secured puts",
                          2: "approved to buy options", 3: "approved for spreads"}


def approval_blocker(plan: ActionPlan, account: BrokerOptionAccountSnapshot) -> OptionDeployBlocker | None:
    """Why the account's option approval does not cover the plan, or ``None`` when it does.

    The level in force is asked, which is the approved level or a lower
    one the owner set at Alpaca. A level Alpaca's answer did not carry is
    not known, and not known is never approval.
    """
    needed = _level_needed(plan)
    what = "A single bought option" if needed == LEVEL_TO_BUY_OPTIONS else "A structure of several legs"
    asks = "the level to buy options (2)" if needed == LEVEL_TO_BUY_OPTIONS else "the spreads level (3)"
    level = account.options_trading_level
    if level is None:
        level = account.options_approved_level
    if level is None:
        return OptionDeployBlocker(
            gate_id=APPROVAL_UNKNOWN,
            label="Options approval",
            headline=(
                f"Alpaca's answer did not say what this account is approved for in options, so this system "
                f"cannot tell. {what} needs {asks}."
            ),
            fix="Check the account's options approval at Alpaca, then review the Deploy again.",
        )
    if level >= needed:
        return None
    approved = account.options_approved_level
    if approved is not None and approved >= needed:
        # Alpaca approved enough. The level in force is lower because it was set lower on the account.
        return OptionDeployBlocker(
            gate_id=APPROVAL_TOO_LOW,
            label="Options approval",
            headline=(
                f"This account's options trading level is set to {level} at Alpaca, below the level {approved} "
                f"it is approved for. {what} needs {asks}."
            ),
            fix="Raise the options trading level in the account's settings at Alpaca. No new approval is needed.",
        )
    return OptionDeployBlocker(
        gate_id=APPROVAL_TOO_LOW,
        label="Options approval",
        headline=f"This account is {_LEVEL_IN_WORDS.get(level, f'at option level {level}')}. {what} needs {asks}.",
        fix=(
            "Ask Alpaca for approval to buy options."
            if needed == LEVEL_TO_BUY_OPTIONS
            else "Ask Alpaca for spreads approval, or choose a single option."
        ),
    )


def buying_power_finding(
    account: BrokerOptionAccountSnapshot, commitment_cents: int | None, structures: int
) -> OptionDeployBlocker | OptionDeployNote | None:
    """Whether the account's option buying power covers one entry, as Alpaca reports the figure.

    Short of it, a blocker that names both figures. Not reported, a note:
    the figure is account-wide and Alpaca's own refusal at the order stays
    the final word, so an unknown here stops nothing.
    """
    if commitment_cents is None:
        return None
    power = account.options_buying_power
    of = plural(structures, "structure")
    if power is None:
        return OptionDeployNote(
            code=BUYING_POWER_UNKNOWN,
            headline="Alpaca did not report this account's options buying power.",
            detail=(
                f"An entry of {of} sets aside {usd(commitment_cents)} right now. Whether the account can cover "
                "it is Alpaca's to say when the order is sent."
            ),
        )
    if power * 100 >= Decimal(commitment_cents):
        return None
    return OptionDeployBlocker(
        gate_id=BUYING_POWER_SHORT,
        label="Options buying power",
        headline=(
            f"Alpaca reports {_in_dollars(power)} of options buying power on this account. An entry of {of} sets "
            f"aside {usd(commitment_cents)} right now."
        ),
        fix="Add cash to the account, close other positions, or deploy fewer structures.",
    )


def _in_dollars(amount: Decimal) -> str:
    """A dollar figure a broker reported, written out: the sign before the ``$``, as cents are written."""
    cents = amount.quantize(Decimal("0.01"))
    return f"-${-cents:,.2f}" if cents < 0 else f"${cents:,.2f}"


async def real_account_findings(
    broker: str, request: AlpacaPaperDeployRequest, plan: ActionPlan, preview: OptionStructurePreview | None
) -> tuple[list[OptionDeployBlocker], list[OptionDeployNote]]:
    """What Alpaca's own answers stop or warn of, for a bot on a Paper or a Live account.

    Two reads of the broker's port, both of its reference and neither of
    market data: the account's option fields, and its listing of the first
    contract the preview picked, when it picked one. A read that failed is
    a blocker that says so. Nothing is deployed on an answer nobody got.
    """
    blockers: list[OptionDeployBlocker] = []
    notes: list[OptionDeployNote] = []
    port = get_broker_registry().resolve(broker)
    try:
        account = await port.get_option_account()
    except BrokerError as exc:
        blockers.append(
            OptionDeployBlocker(
                gate_id=ACCOUNT_NOT_READ,
                label="Options approval",
                headline=sentence(f"Alpaca could not be asked what this account may do in options: {exc.detail or exc}"),
                fix="Try again once Alpaca can be reached.",
            )
        )
    else:
        approval = approval_blocker(plan, account)
        if approval is not None:
            blockers.append(approval)
        commitment = None if preview is None or preview.money is None else preview.money.entry_commitment_cents
        finding = buying_power_finding(account, commitment, request.sizing.quantity)
        if isinstance(finding, OptionDeployBlocker):
            blockers.append(finding)
        elif finding is not None:
            notes.append(finding)
    if preview is not None and preview.legs:
        picked = OptionContract.from_occ_symbol(preview.legs[0].contract.occ_symbol)
        try:
            listing = await port.get_option_contract(picked)
        except BrokerError as exc:
            blockers.append(
                OptionDeployBlocker(
                    gate_id=CONTRACT_NOT_READ,
                    label="Alpaca's listing",
                    headline=sentence(f"Alpaca could not be asked whether it lists {picked.occ_symbol}: {exc.detail or exc}"),
                    fix="Try again once Alpaca can be reached.",
                )
            )
        else:
            refused = option_contract_refusal(picked, listing)
            if refused is not None:
                blockers.append(
                    OptionDeployBlocker(
                        gate_id=refused.reason.value,
                        label="Alpaca's listing",
                        headline=sentence(str(refused)),
                        fix=f"Choose another underlying than {picked.root}, or other rules.",
                    )
                )
    return blockers, notes


# The refusals that say the picked contracts are another shape than the one named.
_SHAPE_REFUSALS: Final = frozenset(str(reason) for reason in StructureRefusal)


def preview_blocker(preview: OptionStructurePreview) -> OptionDeployBlocker | None:
    """What the structure preview finds right now that stops a Deploy: a red verdict on a resolution.

    A leg quoted too wide and a cost over its limit are not this: the bot
    picks again at every entry, so those are said beside the money and stop
    nothing.
    """
    if preview.state != "resolved" or not preview.verdict.red:
        return None
    reason = preview.verdict.resolves_reason_code
    assert reason is not None, "a red verdict on a resolution names why"
    return OptionDeployBlocker(
        gate_id=reason,
        label="Structure",
        headline=preview.verdict.resolves_sentence,
        # Picks that are another shape than the one built are the rules' doing. Money that cannot be set
        # aside (no fee rate pinned for the day, say) is not, and nothing the owner changes is known to help.
        fix=(
            "Change the structure's rules in the builder, then review the Deploy again."
            if reason in _SHAPE_REFUSALS
            else None
        ),
    )


# ---- one entry's price ----------------------------------------------------------------------------------


def price_of(preview: OptionStructurePreview) -> OptionEntryPrice:
    """The price a money review reads from one answer of the structure preview."""
    money = preview.money
    if preview.state == "resolved" and money is not None and not preview.verdict.red:
        return OptionEntryPrice(money.entry_commitment_cents, preview.quotes_read_ms, False, "")
    waiting = preview.ask_again_in_ms is not None
    why = preview.notice or preview.verdict.resolves_sentence
    lead = "Waiting to price this structure." if waiting else "This structure cannot be priced right now."
    return OptionEntryPrice(None, preview.quotes_read_ms, waiting, f"{lead} {why}")


async def read_preview(
    broker: str, account_id: str, request: AlpacaPaperDeployRequest, *, patience_ms: int = 0
) -> OptionStructurePreview:
    """What this Deploy's structure picks and costs right now, from the Clerk's structure preview.

    ``patience_ms`` is how long to go on asking while the answer is only
    still arriving (its quotes, the listing, the rate, the search): none
    for a review, which the page asks again itself, and a few seconds at
    the click. It is a deadline on a clock, counted from the first ask, so
    the time each ask takes is inside it: an ask can itself wait seconds
    for the rate and the dividends. No new ask begins that could not end
    its wait before the deadline.
    """
    asked = preview_request_for(account_id, request)
    deadline_s = time.monotonic() + patience_ms / 1000
    while True:
        answer = await option_structure_panel.option_structure_preview(broker, account_id, asked)
        again = answer.ask_again_in_ms
        if again is None or time.monotonic() + again / 1000 > deadline_s:
            return answer
        await asyncio.sleep(again / 1000)


# ---- the review -------------------------------------------------------------------------------------------


def _money_rows(preview: OptionStructurePreview, structures: int, budget_cents: int | None) -> tuple[OptionMoneyRow, ...]:
    money, at_expiry = preview.money, preview.at_expiry
    if preview.state != "resolved" or money is None:
        return ()
    of = "one structure" if structures == 1 else f"{structures} structures"
    rows = [
        OptionMoneyRow(
            key="set_aside", label=f"Set aside for {of}", amount_cents=money.entry_commitment_cents,
            amount_usd=money.entry_commitment_usd, note=None, part_of_set_aside=False,
        ),
        OptionMoneyRow(
            key="premium", label="Premium", amount_cents=money.net_entry_premium_cents,
            amount_usd=money.premium_usd, note=money.premium_is_label, part_of_set_aside=True,
        ),
        OptionMoneyRow(
            key="broker_requirement", label="Alpaca's requirement", amount_cents=money.broker_requirement_cents,
            amount_usd=money.broker_requirement_usd, note=None, part_of_set_aside=True,
        ),
        OptionMoneyRow(
            key="fees", label="Fees (estimate)", amount_cents=money.fee_estimate_cents,
            amount_usd=money.fee_estimate_usd, note=money.fees_sentence, part_of_set_aside=True,
        ),
    ]
    if at_expiry is not None and at_expiry.estimated and at_expiry.loss_cents is not None:
        rows.append(
            OptionMoneyRow(
                key="loss_at_expiry", label="Loss at expiry, if held, before fees (estimate)",
                amount_cents=at_expiry.loss_cents, amount_usd=at_expiry.loss_usd,
                note=at_expiry.assumptions_sentence, part_of_set_aside=False,
            )
        )
    else:
        rows.append(
            OptionMoneyRow(
                key="loss_at_expiry", label="Loss at expiry, if held, before fees (estimate)", amount_cents=None,
                amount_usd=None,
                note="Not estimated." if at_expiry is None else f"Not estimated. {at_expiry.assumptions_sentence}",
                part_of_set_aside=False,
            )
        )
    if budget_cents is not None:
        free = budget_cents - money.entry_commitment_cents
        rows.append(
            OptionMoneyRow(
                key="budget_free", label="Free in this budget after one entry", amount_cents=free,
                amount_usd=usd(free), note=None, part_of_set_aside=False,
            )
        )
    return tuple(rows)


def _end_sentence(plan: ActionPlan) -> str | None:
    """What a sold leg does to the bot's end: the Clerk closes the position before every session's close it is
    held through (ADR 0076 decision 20). The Clerk owes that close, not the bot's run."""
    if not any(isinstance(leg, OptionEntryLeg) and leg.position == "short" for leg in plan.on_enter):
        return None
    minutes = CloseTerms().sold_leg_close_lead_ms // 60_000
    return (
        f"This structure has a sold leg, so the account's Clerk closes it {minutes} minutes before the close of "
        "every session it is held through, whether or not the bot is running. Keeping it overnight is not offered."
    )


def _standing_notes(request: AlpacaPaperDeployRequest) -> list[OptionDeployNote]:
    """What every option bot on a real account is told before it is deployed."""
    if request.execution_mode == "dry_run":
        return []
    return [
        OptionDeployNote(
            code=UNTESTED_AT_ALPACA,
            headline="This system's option orders have not been checked against Alpaca.",
            detail=(
                "The order, its cancel and its fills are built from Alpaca's published examples, not from "
                "what Alpaca has answered. Check this bot's first option orders at Alpaca yourself."
            ),
        ),
        OptionDeployNote(
            code=FEES_ARE_AN_ESTIMATE,
            headline="Option fees on this account are estimates until Alpaca posts them.",
            detail=(
                "Alpaca reports no fee with a fill. Until its fee rows for a day with an option fill have been "
                "seen, each bot's share of them comes from this system's fee model."
            ),
        ),
        OptionDeployNote(
            code=STREAM_NOT_SEEN,
            headline="If Alpaca's trade stream does not carry option fills, this account stops taking entries at its first option fill.",
            detail=(
                "A fill this system learns only by looking the order up is held as unconfirmed, and an account "
                "with one takes no new entry for any of its bots until it is confirmed. Whether the stream "
                "carries option fills is not known until one Paper order has shown it."
            ),
        ),
    ]


def _preview_notes(preview: OptionStructurePreview) -> list[OptionDeployNote]:
    notes: list[OptionDeployNote] = []
    if preview.state == "resolved" and not preview.verdict.red and preview.entry_refusals:
        notes.append(
            OptionDeployNote(
                code=ENTRY_WOULD_BE_SKIPPED,
                headline=preview.entry_sentence or "An entry would be skipped right now.",
                detail=" ".join(refusal.sentence for refusal in preview.entry_refusals)
                + " The bot picks again at every entry.",
            )
        )
    lean = preview.lean
    if lean is not None and lean.warning_sentence:
        notes.append(
            OptionDeployNote(code=LEANS_AGAINST_THE_SIGNAL, headline=lean.warning_sentence, detail=lean.sentence)
        )
    return notes


def review_of(
    request: AlpacaPaperDeployRequest,
    plan: ActionPlan | None,
    preview: OptionStructurePreview | None,
    *,
    blockers: list[OptionDeployBlocker],
    notes: list[OptionDeployNote],
    budget_cents: int | None,
) -> OptionDeployReview:
    """The review of one option Deploy: its rows, what stops it and what the owner should know.

    ``plan`` is None for a structure no plan can hold; ``blockers`` then
    says why. ``preview`` is the structure preview's answer the review
    stands on, which carries figures only when it resolved. It is None when
    the preview was not asked, and the review then says nothing of quotes
    nobody read: no state of the preview's, no reason code, no sentence.
    """
    choice = _choice(request)
    structures = request.sizing.quantity
    with_figures = preview is not None and preview.state == "resolved"
    return OptionDeployReview(
        underlying=choice.underlying,
        structure_label=label(choice.structure),
        structure_in_a_sentence=in_a_sentence(choice.structure),
        structures=structures,
        orders_summary=orders_in_a_line(
            gap_limit_percent=choice.order_terms.gap_limit_percent,
            entry_wait_seconds=choice.order_terms.entry_wait_seconds,
        ),
        one_structure_explanation=(
            "One structure an entry: each leg's contracts are its ratio, and one contract is 100 shares of "
            f"{choice.underlying}."
        ),
        custom_structures_explanation=(
            "A whole number of structures an entry. Each leg's contracts are that number times its ratio."
        ),
        end_sentence=None if plan is None else _end_sentence(plan),
        preview_state="not_asked" if preview is None else preview.state,
        preview_reason_code=None if preview is None else preview.reason_code,
        preview_sentence=(
            None if preview is None or with_figures else (preview.notice or preview.verdict.resolves_sentence)
        ),
        ask_again_in_ms=None if preview is None else preview.ask_again_in_ms,
        quotes_read_ms=preview.quotes_read_ms if preview is not None and with_figures else None,
        figures_good_until_ms=None if preview is None else preview.figures_good_until_ms,
        picks=() if preview is None else tuple(
            OptionPickedContract(
                number=leg.number, side_label="Buy" if leg.position == "long" else "Sell", ratio=leg.ratio,
                contract=leg.contract,
            )
            for leg in preview.legs
        ),
        money_rows=() if preview is None else _money_rows(preview, structures, budget_cents),
        money_sentence=MONEY_SENTENCE,
        entry_sentence=(
            preview.entry_sentence if preview is not None and with_figures and not preview.verdict.red else None
        ),
        blockers=tuple(blockers),
        notes=tuple([*notes, *([] if preview is None else _preview_notes(preview)), *_standing_notes(request)]),
    )


# ---- Confirm's lines ----------------------------------------------------------------------------------------


def trades_summary(request: AlpacaPaperDeployRequest) -> str:
    """Confirm's "Trades" line for an option bot: what it listens to, what it trades, and how many structures."""
    choice = _choice(request)
    return (
        f"listens to {request.symbol} · trades {in_a_sentence(choice.structure)} {choice.underlying} · "
        f"{plural(request.sizing.quantity, 'structure')}"
    )


# ---- everything that stops it, for the review and for the click ------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Judged:
    """One judging of an option Deploy: its plan when one stands, the structure preview when it was read, and
    what was found."""

    plan: ActionPlan | None
    # None when the structure preview was not asked: no plan stands, this Clerk has no option quotes of its
    # own, or a click was already stopped by something found before it.
    preview: OptionStructurePreview | None
    blockers: list[OptionDeployBlocker]
    notes: list[OptionDeployNote]


async def _judge(
    broker: str,
    account_id: str,
    request: AlpacaPaperDeployRequest,
    *,
    every_blocker: bool,
    patience_ms: int,
    market: OptionMarket | None = None,
) -> _Judged:
    """Ask what a Deploy of this option bot is held to, in the order it is shown.

    First what needs no quote and no broker: a structure no plan can hold,
    one the Clerk does not take, an account whose mode is not open, and a
    Clerk that cannot quote it. A click or an admission read stops at the
    first of those (``every_blocker`` false): nothing is watched and nobody
    is asked for a bot that cannot be deployed.

    The money review goes on, so that its list is whole (``every_blocker``
    true). It reads the structure preview unless no plan stands or this
    Clerk has no option quotes of its own, and it asks Alpaca about a real
    account unless the account's mode is not open. A Live Shadow's review
    asks the live account about option approval, buying power and listing;
    a switched-off Live account's review asks Alpaca nothing. What needs a
    price or a pick (option buying power, Alpaca's listing, a red verdict)
    is found only once the preview has resolved.
    """
    checked = plan_or_refusal(preview_request_for(account_id, request))
    mode = mode_blocker(request)
    if isinstance(checked, PlanRefusal):
        return _Judged(None, None, [plan_blocker(checked), *([] if mode is None else [mode])], [])
    plan = checked
    market = market or this_process_option_market()
    blockers = [found for found in (structure_not_taken(request, plan), mode) if found is not None]
    blockers.extend(quote_line_blockers(plan, market))
    if blockers and not every_blocker:
        return _Judged(plan, None, blockers, [])
    preview = None
    if not market.quotes_are_read_elsewhere():
        preview = await read_preview(broker, account_id, request, patience_ms=patience_ms)
    notes: list[OptionDeployNote] = []
    if request.execution_mode != "dry_run" and mode is None:
        said_by_alpaca, notes = await real_account_findings(broker, request, plan, preview)
        blockers.extend(said_by_alpaca)
    found = None if preview is None else preview_blocker(preview)
    if found is not None:
        blockers.append(found)
    return _Judged(plan, preview, blockers, notes)


def _price(judged: _Judged) -> OptionEntryPrice:
    """The price a budget is judged against: none for a Deploy that something stops, in the first blocker's words."""
    if not judged.blockers:
        assert judged.preview is not None
        return price_of(judged.preview)
    read_ms = now_ms_utc() if judged.preview is None else judged.preview.quotes_read_ms
    return OptionEntryPrice(None, read_ms, False, judged.blockers[0].headline)


async def review_for(
    broker: str, account_id: str, request: AlpacaPaperDeployRequest, *, budget_cents: int | None
) -> tuple[OptionDeployReview, OptionEntryPrice]:
    """The review of one option Deploy, and the price its budget is judged against.

    Every blocker found is listed. A Deploy that something stops has no
    price: its budget is reviewed against nothing, so no review of it can be
    consented to.
    """
    judged = await _judge(broker, account_id, request, every_blocker=True, patience_ms=0)
    review = review_of(
        request, judged.plan, judged.preview, blockers=judged.blockers, notes=judged.notes, budget_cents=budget_cents
    )
    return review, _price(judged)


def require_mode_open(request: AlpacaPaperDeployRequest) -> None:
    """Raise the refusal of an account whose mode takes no option bot, by name.

    Asked at the click before the owner's acceptance is: nobody is asked to
    write an acceptance, kept with a bot for good, for a bot that cannot
    exist.
    """
    blocked = mode_blocker(request)
    if blocked is not None:
        raise _refused(blocked)


async def require_deployable(
    broker: str, account_id: str, request: AlpacaPaperDeployRequest, *, at_the_click: bool
) -> tuple[ActionPlan, OptionEntryPrice]:
    """The plan and the price of an option Deploy that nothing stops, or the first thing that stops it, by name.

    At the click a preview that is only still arriving is waited for, for
    ``PRICE_PATIENCE_MS``, and a Deploy with no price is then refused by
    name (``NOT_PRICED``) in the price's own sentence, with a budget on the
    request or without one, on a Dry Run and on a real account: an option
    bot is deployed only while one entry of its structure can be priced
    (ADR 0076 decision 24). So every click that gets past this was held to
    what needs a price or a pick too: Alpaca's listing, option buying power
    and a red verdict.

    A read made before the click waits for nothing and is not refused for a
    price that has not arrived: the page asks it again.
    """
    judged = await _judge(
        broker, account_id, request, every_blocker=False, patience_ms=PRICE_PATIENCE_MS if at_the_click else 0
    )
    if judged.blockers:
        raise _refused(judged.blockers[0])
    assert judged.plan is not None
    price = _price(judged)
    if at_the_click and price.commitment_cents is None:
        raise _refusal(
            NOT_PRICED,
            price.detail,
            detail=(
                "An option bot is deployed only while one entry of its structure can be priced. Nothing was "
                "deployed and nothing was sent."
            ),
            next_action=(
                "Deploy again in a few seconds." if price.waiting else "Review the Deploy again: its review says why."
            ),
        )
    return judged.plan, price


__all__ = [
    "LIVE_SWITCHED_OFF",
    "OptionEntryPrice",
    "approval_blocker",
    "buying_power_finding",
    "mode_blocker",
    "plan_blocker",
    "preview_blocker",
    "preview_request_for",
    "price_of",
    "quote_line_blockers",
    "read_preview",
    "real_account_findings",
    "require_deployable",
    "require_mode_open",
    "review_for",
    "review_of",
    "structure_not_taken",
    "trades_summary",
]
