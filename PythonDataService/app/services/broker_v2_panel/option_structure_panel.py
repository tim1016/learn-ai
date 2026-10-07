"""The Deploy form's two option reads on a Clerk: the named-structure catalogue, and the preview (#2913).

Transport-facing wrappers only. What a preview is lives in
``app.services.option_structure_preview``; what a named structure starts
from lives in ``app.options.structure_catalogue``.
"""

from __future__ import annotations

from datetime import date

from app.broker.alpaca.market_liveness import get_option_quotes
from app.broker.ibkr.config import get_settings as ibkr_settings
from app.options.named_structures import NamedStructure
from app.options.structure import MAX_OPTION_LEGS
from app.options.structure_catalogue import (
    EXPIRY_RULES,
    GROUPS,
    STRIKE_RULES,
    RuleOffered,
    description,
    label,
    order_term_starts,
    starting_plan,
)
from app.schemas.action_plan import OptionEntryLeg
from app.schemas.option_preview import (
    NamedStructureGroup,
    NamedStructureStart,
    OptionExpiryRule,
    OptionLegRules,
    OptionOrderTermDefaults,
    OptionPercentTerm,
    OptionRuleTolerance,
    OptionRuleValue,
    OptionStrikeRule,
    OptionStructureCatalogue,
    OptionStructurePreview,
    OptionStructurePreviewRequest,
    OptionWholeNumberTerm,
)
from app.services.broker_v2_panel.panel_scope import validate_account
from app.services.option_model_inputs import ModelInputsRead, read_option_model_inputs
from app.services.option_search import PreviewSearches, get_option_searches
from app.services.option_structure_preview import preview_option_structure
from app.services.option_watch import get_option_watch_list
from app.utils.timestamps import now_ms_utc

# The catalogue's legs carry no instrument: the builder puts them on whatever the owner picks. A starting
# plan needs one to be a plan at all, and this stands in for it.
_ANY_UNDERLYING = "SPY"


def _model_inputs(underlying: str, at_ms: int, latest_expiry: date) -> ModelInputsRead:
    # A page asks every second or so. It never queues behind a lookup another ask already started.
    return read_option_model_inputs(underlying, at_ms=at_ms, latest_expiry=latest_expiry, wait=False)


def _preview_searches() -> PreviewSearches:
    """Where a page's search for its contracts runs: this process's preview lane, a worker process of its own."""
    return get_option_searches().preview


async def option_structure_preview(
    broker: str, account_id: str, request: OptionStructurePreviewRequest
) -> OptionStructurePreview:
    """What the structure the owner is building would pick right now on this account's Clerk, and its figures."""
    await validate_account(broker, account_id)
    return await preview_option_structure(
        request,
        quotes=get_option_quotes(),
        watch=get_option_watch_list(),
        model_inputs=_model_inputs,
        searches=_preview_searches(),
        strikes_each_side=ibkr_settings().option_window_strikes_each_side,
        clock=now_ms_utc,
    )


def _start(name: NamedStructure) -> NamedStructureStart:
    legs = [leg for leg in starting_plan(name, _ANY_UNDERLYING).on_enter if isinstance(leg, OptionEntryLeg)]
    return NamedStructureStart(
        structure=name,
        label=label(name),
        description=description(name),
        legs=[
            OptionLegRules(
                leg_id=leg.leg_id,
                position=leg.position,
                qty_ratio=leg.qty_ratio,
                right=leg.right,
                strike=leg.strike,
                expiry=leg.expiry,
            )
            for leg in legs
        ],
    )


def _value(offered: RuleOffered) -> OptionRuleValue | None:
    if offered.value_field is None:
        return None
    return OptionRuleValue(field=offered.value_field, unit=offered.value_unit, unit_label=offered.value_label)


def _tolerance(offered: RuleOffered) -> OptionRuleTolerance | None:
    if offered.tolerance_field is None:
        return None
    return OptionRuleTolerance(
        field=offered.tolerance_field,
        unit=offered.tolerance_unit,
        unit_label=offered.tolerance_label,
        default=offered.tolerance_default,
    )


def _order_terms() -> OptionOrderTermDefaults:
    starts = order_term_starts()

    def percent(name: str) -> OptionPercentTerm:
        return OptionPercentTerm(label=starts[name].label, unit_label=starts[name].unit_label, default=starts[name].default)

    def whole(name: str) -> OptionWholeNumberTerm:
        return OptionWholeNumberTerm(
            label=starts[name].label, unit_label=starts[name].unit_label, default=int(starts[name].default)
        )

    return OptionOrderTermDefaults(
        gap_limit_percent=percent("gap_limit_percent"),
        entry_wait_seconds=whole("entry_wait_seconds"),
        cost_limit_percent=percent("cost_limit_percent"),
        expiry_close_lead_minutes=whole("expiry_close_lead_minutes"),
    )


async def option_structure_catalogue(broker: str, account_id: str) -> OptionStructureCatalogue:
    """What the builder offers: the named structures in its groups, each with the legs it starts from; the
    rules a leg may be given; and the order terms a Deploy starts from."""
    await validate_account(broker, account_id)
    return OptionStructureCatalogue(
        groups=[
            NamedStructureGroup(label=group, structures=[_start(name) for name in names]) for group, names in GROUPS
        ],
        custom_label="Custom",
        custom_description=f"Start from no legs and add up to {MAX_OPTION_LEGS}.",
        max_legs=MAX_OPTION_LEGS,
        strike_rules=[
            OptionStrikeRule(selector=offered.selector, label=offered.label, value=_value(offered), tolerance=_tolerance(offered))
            for offered in STRIKE_RULES
        ],
        expiry_rules=[
            OptionExpiryRule(selector=offered.selector, label=offered.label, value=_value(offered)) for offered in EXPIRY_RULES
        ],
        order_terms=_order_terms(),
    )
