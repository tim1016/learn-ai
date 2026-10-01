"""Golden Search operator copy: closed maps from a study's situation to the sentences the workbench shows (#2696).

Angular renders these strings; it never composes them. Each map is keyed by
a state, a stop reason, a candidate's situation or a finding code, so a new
situation is a code change here, never free text built in the browser.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from app.research.golden_search.declarations import SearchDeclaration, knob_values
from app.research.golden_search.protocol import ExecutionAssumptions, KnobPlan, Method

# ── Study guidance by state (§4.6) ───────────────────────────────────────

WEAK_EVIDENCE_DETAIL = (
    "You can keep the incumbent, or approve with a written reason and explicit acceptance of the weak evidence."
)
_STATE_GUIDANCE: dict[str, tuple[str, str]] = {
    "locked": ("Ready to search", "The final test stays sealed through every development step."),
    "search_running": ("Searching the development period", "Only development data is read. The final test stays sealed."),
    "awaiting_validation": (
        "Understand where the search went",
        "Zoom proposes a local improvement. Grid checks whether interacting knobs tell a different story.",
    ),
    "validation_running": (
        "Testing the selection procedure over time",
        "Each fold searches only its own past, from the original ranges and starting rules.",
    ),
    "awaiting_candidate": (
        "A better fit is only the beginning",
        "Compare the return with the losses you had to sit through. Then look for fragile settings.",
    ),
    "candidate_locked": (
        "One candidate. One final look.",
        "The choice becomes fixed when you open the test. Changing your mind afterwards needs fresh evidence.",
    ),
    "exam_running": (
        "Running the final test once",
        "Only the locked candidate and the frozen incumbent run on the held-back interval.",
    ),
    "qualification_pending": (
        "Building the proof and publishing the version",
        "Approval builds and verifies proof, records your review and publishes the exact version. "
        "Technical failure preserves the old default.",
    ),
    "approved": (
        "Golden configuration ready in Deploy",
        "Use this exact version for Paper or Live. Your evidence and acknowledgements travel with it.",
    ),
    "closed": ("Study closed", "The study and its trial history remain available."),
}
_QUALIFICATION_FAILED_DETAIL = (
    "Resolve the problem and retry this approval. Research override cannot bypass this failure; "
    "the existing default is unchanged."
)
_STOPPED_STAGE = (
    "This stage stopped before it finished",
    "Finish resumes it: every evaluation already recorded is reused, and no budget is spent twice.",
)
_QUEUED_STAGE = ("Waiting for a worker", "The stage is authorized and will start when a worker picks it up.")


def study_guidance(
    *,
    state: str,
    presented_status: str,
    exam_outcome: str | None,
    claim: str | None,
    exposure_state: str | None,
    exam_locked: bool,
    failure_reason: str | None,
) -> dict[str, str]:
    """The decision to make now, as ``{headline, detail}``."""
    if state in ("search_running", "validation_running", "exam_running", "qualification_pending"):
        if presented_status in ("failed", "cancelled", "interrupted"):
            headline, detail = _STOPPED_STAGE
            return {"headline": headline, "detail": failure_reason or detail}
        if presented_status == "queued":
            headline, detail = _QUEUED_STAGE
            return {"headline": headline, "detail": detail}
    if state == "awaiting_review":
        return {"headline": _review_headline(exam_outcome, claim, exposure_state), "detail": _review_detail(exam_outcome, claim)}
    if state == "qualification_failed":
        return {"headline": "Qualification failed · current default unchanged", "detail": failure_reason or _QUALIFICATION_FAILED_DETAIL}
    if state == "retained":
        tail = " The opened final test remains recorded as exposed." if exam_locked else " The final test has not been opened."
        return {"headline": "Current settings retained", "detail": "The study and its trial history remain available." + tail}
    headline, detail = _STATE_GUIDANCE[state]
    return {"headline": headline, "detail": detail}


def _review_headline(exam_outcome: str | None, claim: str | None, exposure_state: str | None) -> str:
    """First match in the order the cases are listed: a clean pass, thin evidence, a reused or unknown interval, a miss."""
    if exam_outcome == "meets_rules" and claim == "confirmatory":
        return "Approve the settings you want to use"
    if exam_outcome == "not_enough_evidence":
        return "Not enough evidence to recommend a change"
    if exposure_state == "previously_used":
        return "This test was already used"
    if exposure_state == "history_unknown":
        return "This test's history is unknown"
    return "Keeping your current settings is recommended"


def _review_detail(exam_outcome: str | None, claim: str | None) -> str:
    if exam_outcome == "meets_rules" and claim == "confirmatory":
        return (
            "The candidate meets your research rules. Approval builds the proof and makes the qualified settings "
            "available in Deploy."
        )
    return WEAK_EVIDENCE_DETAIL


def research_weakness(exam_outcome: str | None, claim: str | None, exposure_state: str | None) -> list[str]:
    """The weaknesses an approval must acknowledge explicitly; empty when the evidence meets the rules."""
    weakness: list[str] = []
    if exam_outcome != "meets_rules":
        weakness.append(f"EXAM_{(exam_outcome or 'missing').upper()}")
    if claim != "confirmatory":
        weakness.append(f"EXPOSURE_{(exposure_state or 'unknown').upper()}")
    return weakness


# ── Stop explanations ────────────────────────────────────────────────────

KNOB_STOP_COPY: dict[str, str] = {
    "no_improvement": "No better tested move",
    "quantization_limit": "Minimum step reached",
    "budget": "Budget reached",
    "no_eligible": "No setting met the rules",
    "pass_limit": "Pass limit reached",
}
_GRID_KNOB_STOP_COPY: dict[str, str] = {**KNOB_STOP_COPY, "no_improvement": "Every listed value tested"}


def knob_stop_explanation(method: Method, reason: str) -> str:
    return (_GRID_KNOB_STOP_COPY if method == "grid" else KNOB_STOP_COPY)[reason]


# ── Candidates ───────────────────────────────────────────────────────────

_GUIDANCE: dict[str, tuple[str, str]] = {
    "incumbent": (
        "No change can be the best decision",
        "Keeping the incumbent is a complete research decision. It avoids replacing known settings on weak evidence. "
        "Use Keep current settings to finish without consuming the final test.",
    ),
    "same_as_incumbent": (
        "The search returned the current settings",
        "A final test would only re-check them. Use Keep current settings to finish without consuming the final test.",
    ),
    "drawdown_warning": (
        "The extra return comes with a warning",
        "Its worst drawdown in the development replay is above your ceiling. Keeping it as exploration is the cautious choice.",
    ),
    "fragile": (
        "Nearby settings lose money",
        "Settings one step away lose money in the development replay. Inspect that sensitivity before using your final test.",
    ),
    "not_evaluated": (
        "This candidate was not evaluated",
        "The evaluation budget ran out before its development run, so its evidence is incomplete.",
    ),
    "ineligible": (
        "This candidate does not meet your rules",
        "It fails one of your development rules. A final test can still run, but it would start from weak evidence.",
    ),
    "robust": (
        "Prefer evidence that survives small changes",
        "Its neighbors one step away were tested too. Surviving small changes is a reason to investigate, "
        "not proof of a durable edge.",
    ),
    "unaudited": (
        "Check how fragile it is",
        "This candidate meets your rules in the development replay. Its neighbors were not audited, so its "
        "sensitivity to small changes is unknown.",
    ),
}
_RULE_FLAGS: dict[str, str] = {
    "NOT_EVALUATED": "Not evaluated",
    "FAILED": "Run failed",
    "NO_TRADES": "No trades",
    "OBJECTIVE_UNDEFINED": "Objective undefined",
    "DRAWDOWN_UNDEFINED": "Drawdown unavailable",
    "NOT_PROFITABLE": "Not profitable",
}
_FINDING_FLAGS: dict[str, str] = {
    "SAME_AS_INCUMBENT": "Same as current settings",
    "NEIGHBORS_LOSE_MONEY": "Nearby settings lose money",
    "EDGE_OF_RANGE": "At the edge of the searched range",
    "STRESS_TURNS_NEGATIVE": "Extra costs erase its profit",
}


def candidate_guidance(
    *, key: str, same_as_incumbent: bool, ineligibility: str | None, losing_neighbors: bool, neighbors_audited: bool
) -> dict[str, str]:
    if key == "incumbent":
        situation = "incumbent"
    elif same_as_incumbent:
        situation = "same_as_incumbent"
    elif ineligibility == "DRAWDOWN_ABOVE_CEILING":
        situation = "drawdown_warning"
    elif ineligibility == "NOT_EVALUATED":
        situation = "not_evaluated"
    elif losing_neighbors:
        situation = "fragile"
    elif ineligibility is not None:
        situation = "ineligible"
    elif neighbors_audited:
        situation = "robust"
    else:
        situation = "unaudited"
    title, text = _GUIDANCE[situation]
    return {"title": title, "text": text}


def candidate_flags(
    *,
    ineligibility: str | None,
    total_trades: int | None,
    min_trades: int,
    drawdown_ceiling: float,
    finding_codes: Sequence[str],
) -> list[dict[str, str]]:
    """Short labels for what weakens a candidate: its first failed rule, then its own findings."""
    flags: list[dict[str, str]] = []
    if ineligibility == "DRAWDOWN_ABOVE_CEILING":
        flags.append({"code": ineligibility, "text": f"Above {drawdown_ceiling:.0%} limit"})
    elif ineligibility == "TOO_FEW_TRADES":
        flags.append({"code": ineligibility, "text": f"{total_trades} trades, below {min_trades}"})
    elif ineligibility is not None:
        flags.append({"code": ineligibility, "text": _RULE_FLAGS[ineligibility]})
    for code in dict.fromkeys(finding_codes):
        if code in _FINDING_FLAGS:
            flags.append({"code": code, "text": _FINDING_FLAGS[code]})
    return flags


# ── Sentences ────────────────────────────────────────────────────────────


def _number(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _ema_sentence(values: Mapping[str, Decimal]) -> str:
    return (
        f"Gap ${values['gap']:.2f} · RSI {_number(values['rsi_min'])}–{_number(values['rsi_max'])} · "
        f"EMA {_number(values['fast_period'])}/{_number(values['slow_period'])} · hold {_number(values['hold_bars'])} bars"
    )


_SENTENCES = {"ema_crossover_signal": _ema_sentence}
# Short names for fixed knobs, where the declaration's label reads awkwardly in a sentence.
_FIXED_NAMES: dict[tuple[str, str], tuple[str, str]] = {("ema_crossover_signal", "gap_bps"): ("Normalized gap", "bps")}


def params_sentence(declaration: SearchDeclaration, point: Mapping[str, Any]) -> str:
    """The whole tuple at a glance, e.g. ``Gap $0.15 · RSI 48–72 · EMA 8/21 · hold 4 bars``."""
    values = knob_values(declaration, point)
    custom = _SENTENCES.get(declaration.strategy_key)
    if custom is not None:
        return custom(values)
    return " · ".join(f"{knob.label} {_number(values[knob.name])} {knob.unit}" for knob in declaration.knobs)


def fixed_sentence(declaration: SearchDeclaration, plans: Sequence[KnobPlan]) -> str:
    """The knobs the plan held fixed, e.g. ``Normalized gap fixed at 0 bps``; empty when none were."""
    parts = []
    for plan in plans:
        if plan.mode != "fixed":
            continue
        knob = declaration.knob(plan.name)
        name, unit = _FIXED_NAMES.get((declaration.strategy_key, plan.name), (knob.label, knob.unit))
        parts.append(f"{name} fixed at {_number(Decimal(str(plan.fixed_value)))} {unit}")
    return " · ".join(parts)


_FILL_MODE_COPY = {
    "decision_minute_open": "fills at the decision minute's open",
    "next_bar_open": "fills at the next bar's open",
    "signal_bar_close": "fills at the signal bar's close",
}


def costs_sentence(execution: ExecutionAssumptions) -> str:
    """Flat stated costs, never a historical broker fee schedule."""
    return (
        f"${execution.commission_per_order:,.2f} per order · ${execution.slippage_per_share:,.2f}/share slippage · "
        f"{_FILL_MODE_COPY[execution.fill_mode]}. Historical broker fees are not modeled."
    )


def validation_explanation(based_on: str | None, folds: int) -> str:
    coverage = f" The legacy verdict is {based_on}." if based_on else ""
    return (
        f"Each of the {folds} folds searched only its own training window, from the original ranges and starting point, "
        "then ran its winner and the frozen incumbent once on the test window that followed. The linked line joins "
        "those test returns with fresh capital each fold; it is the procedure's record, not a fixed candidate's."
        + coverage
    )
