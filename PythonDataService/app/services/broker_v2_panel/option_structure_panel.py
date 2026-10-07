"""The Deploy form's two option reads on a Clerk: the named-structure catalogue, and the preview (#2913).

Transport-facing wrappers only. What a preview is lives in
``app.services.option_structure_preview``; what a named structure starts
from lives in ``app.options.structure_catalogue``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from app.broker.alpaca.market_liveness import get_option_quotes
from app.broker.ibkr.config import get_settings as ibkr_settings
from app.options.bounds import Bounds, bounds_of
from app.options.named_structures import NamedStructure
from app.options.structure import MAX_OPTION_LEGS
from app.options.structure_catalogue import (
    ALPACA_SENTENCE,
    CUSTOM_LABEL,
    ENTRIES_SENTENCE,
    EXITS_SENTENCE,
    EXPIRY_RULES,
    GROUPS,
    HOURS_SENTENCE,
    STRIKE_RULES,
    RuleOffered,
    description,
    in_a_sentence,
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
    OptionOrderTermNotes,
    OptionPercentTerm,
    OptionRuleOtherLeg,
    OptionRuleTolerance,
    OptionRuleValue,
    OptionStrikeRule,
    OptionStructureCatalogue,
    OptionStructurePreview,
    OptionStructurePreviewRequest,
    OptionValueBounds,
    OptionWholeNumberTerm,
)
from app.services.broker_v2_panel.panel_scope import validate_account
from app.services.option_model_inputs import ModelInputsRead, read_option_model_inputs
from app.services.option_preview_out_of_range import numbers_out_of_range
from app.services.option_preview_words import bounds_in_words, fixed_dates_taken_in_words
from app.services.option_search import PreviewSearches, get_option_searches
from app.services.option_structure_preview import preview_of_numbers_out_of_range, preview_option_structure
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


async def option_structure_out_of_range(
    broker: str, account_id: str, body: object, errors: Sequence[Mapping[str, Any]]
) -> OptionStructurePreview | None:
    """The answer to a preview request that its own validation refused, when every fault in it is a number
    outside the range the catalogue gives for it. ``None`` when the request is malformed some other way."""
    refused = numbers_out_of_range(body, errors)
    if refused is None:
        return None
    await validate_account(broker, account_id)
    return preview_of_numbers_out_of_range(refused, watch=get_option_watch_list(), clock=now_ms_utc)


def _start(name: NamedStructure) -> NamedStructureStart:
    legs = [leg for leg in starting_plan(name, _ANY_UNDERLYING).on_enter if isinstance(leg, OptionEntryLeg)]
    return NamedStructureStart(
        structure=name,
        label=label(name),
        in_a_sentence=in_a_sentence(name),
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


def _bounds(bounds: Bounds) -> OptionValueBounds:
    """A number's range as a form is served it."""
    return OptionValueBounds(
        min=bounds.lowest,
        min_included=bounds.lowest_included,
        max=bounds.highest,
        max_included=bounds.highest_included,
        min_ms=None,
        max_ms=None,
        step=bounds.step,
        sentence=bounds_in_words(bounds),
    )


def _date_bounds(bounds: Bounds) -> OptionValueBounds:
    """A date's range as a form is served it: its two ends as moments, ``int64 ms UTC``, and said in dates.

    A time is never served as text, so the ends do not go in the two fields
    a number's range uses.
    """
    if bounds.lowest is None or bounds.highest is None:
        raise ValueError(f"a date's range has a first date and a last; got {bounds}")
    return OptionValueBounds(
        min=None,
        min_included=bounds.lowest_included,
        max=None,
        max_included=bounds.highest_included,
        min_ms=int(bounds.lowest),
        max_ms=int(bounds.highest),
        step=None,
        sentence=fixed_dates_taken_in_words(),
    )


def _value(offered: RuleOffered) -> OptionRuleValue | None:
    if offered.value_field is None or offered.value_bounds is None:
        return None
    return OptionRuleValue(
        field=offered.value_field,
        unit=offered.value_unit,
        unit_label=offered.value_label,
        bounds=_date_bounds(offered.value_bounds) if offered.value_unit == "date" else _bounds(offered.value_bounds),
    )


def _measured_from(offered: RuleOffered) -> OptionRuleOtherLeg | None:
    if offered.other_leg_field is None:
        return None
    return OptionRuleOtherLeg(field=offered.other_leg_field, unit="leg", unit_label=offered.other_leg_label)


def _tolerance(offered: RuleOffered) -> OptionRuleTolerance | None:
    if offered.tolerance_field is None or offered.tolerance_bounds is None:
        return None
    return OptionRuleTolerance(
        field=offered.tolerance_field,
        unit=offered.tolerance_unit,
        unit_label=offered.tolerance_label,
        default=offered.tolerance_default,
        bounds=_bounds(offered.tolerance_bounds),
    )


def _order_terms() -> OptionOrderTermDefaults:
    starts = order_term_starts()

    def percent(name: str) -> OptionPercentTerm:
        start = starts[name]
        return OptionPercentTerm(
            label=start.label, unit_label=start.unit_label, default=start.default, bounds=_bounds(start.bounds),
            help=start.help, range_reason=start.range_reason,
        )

    def whole(name: str) -> OptionWholeNumberTerm:
        start = starts[name]
        return OptionWholeNumberTerm(
            label=start.label, unit_label=start.unit_label, default=int(start.default), bounds=_bounds(start.bounds),
            help=start.help, range_reason=start.range_reason,
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
        custom_label=CUSTOM_LABEL,
        custom_in_a_sentence=in_a_sentence(None),
        custom_description=f"Start from no legs and add up to {MAX_OPTION_LEGS}.",
        max_legs=MAX_OPTION_LEGS,
        leg_quantity_bounds=_bounds(bounds_of(OptionLegRules, "qty_ratio")),
        structures_bounds=_bounds(bounds_of(OptionStructurePreviewRequest, "structures")),
        strike_rules=[
            OptionStrikeRule(
                selector=offered.selector,
                label=offered.label,
                value=_value(offered),
                measured_from=_measured_from(offered),
                tolerance=_tolerance(offered),
            )
            for offered in STRIKE_RULES
        ],
        expiry_rules=[
            OptionExpiryRule(selector=offered.selector, label=offered.label, value=_value(offered)) for offered in EXPIRY_RULES
        ],
        order_terms=_order_terms(),
        order_term_notes=OptionOrderTermNotes(
            entries_sentence=ENTRIES_SENTENCE,
            exits_sentence=EXITS_SENTENCE,
            hours_sentence=HOURS_SENTENCE,
            alpaca_sentence=ALPACA_SENTENCE,
        ),
    )
