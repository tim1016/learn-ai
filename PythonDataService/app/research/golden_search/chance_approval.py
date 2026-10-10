"""The chance-evidence approval policy: what an enrolled study's approval must acknowledge (#3015, ADR 0074 decision 12).

Formula: a study locks an immutable enrollment marker beside its plan
  (``results["chance_approval"]``, one versioned dict naming the required
  final components and the threshold semantics). At approval intent, at the
  qualification payload and at the page's weakness items, one authority —
  :func:`chance_weakness_items` — reads the chosen fit's stored final chance
  components and returns the acknowledgement items: ``CHANCE_WITHIN_NOISE``
  when a measured required share sits below that reading's own stored clear
  threshold (equality is clear; raw counts decide, never rounded shares),
  and ``CHANCE_UNAVAILABLE`` when an enrolled study's required component is
  missing, unsupported, corrupt, without a finite usable sample, or without
  its stored interpretation basis. Carry-over ranks and the rehearsal's 80%
  target never decide these items, and the optional fit-to-fit and Compare
  drawdown components never gate an approval.
Reference: issue #3015's delivery contract (the 2026-10-09 adversarial
  review of #2932, owner decision of 2026-10-09); the thresholds are the
  stored ones each #3011/#3014 reading froze into its own basis.
Canonical implementation: this file for the acknowledgement items and the
  enrollment policy; ``chance_final.stored_final_chance`` for the normalized
  components it reads; ``chance_compare`` for the readings' shapes.
Validated against: tests/research/golden_search/test_chance_approval.py
  (threshold equality is clear, raw counts decide, no finite sample,
  missing/failed/unsupported components, legacy studies, positivity off,
  and the carry-over's never-decides rule) and the service tests in
  test_chance_approval_service.py; #3024 adds the forged-label and
  inclusive-count fixtures, where the raw counts overrule the stored
  label and the stored inclusive count.

A study locked before this policy shipped carries no marker and keeps the
original absent-reading behavior: a measured weak share still asks for the
acknowledgement (owner decision 3), but nothing missing ever gates. A
future change of constants cannot reclassify an existing reading — the
thresholds are the reading's own, never today's — and a marker is never
inferred from dates on read.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.research.golden_search.chance_compare import _at_least

#: What the enrollment marker (and the approval gate it names) call themselves.
SCHEMA = "golden-search-chance-approval/v1"

#: The final-test components an enrolled approval requires; the optional ones are informational and never gate.
REQUIRED_COMPONENTS = ("comparison", "profitability", "carry_over")
INFORMATIONAL_COMPONENTS = ("fit_to_fit", "compare_drawdown")

CHANCE_WITHIN_NOISE = "CHANCE_WITHIN_NOISE"
CHANCE_UNAVAILABLE = "CHANCE_UNAVAILABLE"


def enrollment_policy() -> dict[str, Any]:
    """The immutable marker a new study locks with: which components an approval requires and how weakness reads.

    Stored once beside the plan, never recomputed: a reader years later sees the rules this study enrolled
    under, not today's.
    """
    return {
        "schema_version": SCHEMA,
        "required_final_components": list(REQUIRED_COMPONENTS),
        "conditional": {"profitability": "required when the plan requires a positive net result"},
        "informational": list(INFORMATIONAL_COMPONENTS),
        "weakness": {
            "code": CHANCE_WITHIN_NOISE,
            "rule": "A measured required share below that reading's own stored clear threshold is weak; at-or-above it is clear. Raw counts decide; a rounded share never does.",
        },
        "unavailable": {
            "code": CHANCE_UNAVAILABLE,
            "rule": "A required component that is missing, unsupported, corrupt, has no finite usable sample, or lacks its stored interpretation basis must be acknowledged as unavailable. An unavailable comparison is not clear evidence against a fit.",
        },
    }


def _stored_threshold(record: Mapping[str, Any]) -> float | None:
    thresholds = (record.get("basis") or {}).get("thresholds") or {}
    value = thresholds.get("clear")
    return value if isinstance(value, (int, float)) else None


def _replications(record: Mapping[str, Any]) -> int | None:
    value = (record.get("basis") or {}).get("replications")
    return value if isinstance(value, int) and value > 0 else None


def _unavailable(component: str, reason: str) -> dict[str, str]:
    # Noun-phrase copy: the acknowledgement reads "I approve despite <text>", so every item completes that
    # sentence instead of starting a new one.
    return {"code": CHANCE_UNAVAILABLE, "text": f"an unavailable required {component} reading ({reason})"}


def _reason_of(record: Mapping[str, Any]) -> str:
    # A failed record's operator sentence names what could not be computed; a missing record's reason names the
    # named missing input. Either way the words are the reading's own, never invented here.
    for key in ("sentence", "reason"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().rstrip(".")
    return "no reason recorded"


def _share_item(component: str, label: str, record: Mapping[str, Any], *, numerator: int, enrolled: bool) -> dict[str, str] | None:
    """One measured share-bearing component's item: weak below its own stored threshold, nothing at-or-above it.

    A reading without its stored threshold is unavailable for enrolled studies — today's constants are never
    borrowed for a reading that lacks them, and a legacy study simply has nothing to read.
    """
    threshold = _stored_threshold(record)
    replications = _replications(record)
    if threshold is None or replications is None:
        return _unavailable(component, "its stored interpretation basis holds no clear threshold to read") if enrolled else None
    if not _at_least(numerator, replications, threshold):  # the same exact comparison #3011 owns, inverted
        return {
            "code": CHANCE_WITHIN_NOISE,
            "text": f"{label} inside the resampling model's noise: {numerator:,} of {replications:,} paths ({numerator / replications:.1%}), below the {threshold:.0%} clear threshold the reading stored",
        }
    return None


def _numerators(record: Mapping[str, Any], defined_keys: tuple[str, ...], numerator_keys: tuple[str, ...], fallback: Any) -> tuple[int, int]:
    """One share-bearing component's numerator and defined-path count, derived from its raw counts (#3024).

    The stored label and the stored inclusive count are presentation of the counts, never authority: a forged
    clear label over all-undefined counts reads as what its counts say. Records reach here already validated
    by ``chance_final.stored_final_chance`` in production; the fallbacks keep direct callers readable.
    """
    counts = record.get("counts")
    if isinstance(counts, Mapping) and all(isinstance(counts.get(key), int) and not isinstance(counts.get(key), bool) for key in (*defined_keys, "undefined")):
        return sum(counts[key] for key in numerator_keys), sum(counts[key] for key in defined_keys)
    return (fallback if isinstance(fallback, int) and not isinstance(fallback, bool) else 0), 1  # no readable counts: never read as no_defined_paths


def chance_weakness_items(
    policy: Mapping[str, Any] | None,
    chance: Mapping[str, Mapping[str, Any]],
    *,
    positive_net_required: bool,
) -> list[dict[str, str]]:
    """The chance acknowledgement items for approving one fit, from its stored final components — one authority.

    ``policy`` is the study's enrollment marker; ``None`` is a pre-enrollment study, which keeps the original
    absent-reading behavior (a measured weak share still asks, nothing missing ever gates). ``chance`` is the
    normalized per-component record :func:`chance_final.stored_final_chance` serves — the server's own stored
    evidence, never client values. Measured shares are read from the raw counts the record carries; the label
    and the stored inclusive count never decide anything (#3024).
    """
    enrolled = policy is not None
    items: list[dict[str, str]] = []

    comparison = chance.get("comparison") or {}
    if comparison.get("status") == "measured":
        at_least = comparison.get("at_least_as_good")
        as_good, defined = _numerators(comparison, ("better", "tied", "worse"), ("better", "tied"), at_least.get("count") if isinstance(at_least, Mapping) else 0)
        if enrolled and defined == 0:
            items.append(_unavailable("comparison", "no resampled path produced a defined result to compare"))
        else:
            item = _share_item("comparison", "support against the current settings", comparison, numerator=as_good, enrolled=enrolled)
            if item is not None:
                items.append(item)
    elif enrolled:
        items.append(_unavailable("comparison", _reason_of(comparison)))

    if positive_net_required:
        profitability = chance.get("profitability") or {}
        if profitability.get("status") == "measured":
            positive, defined = _numerators(profitability, ("positive", "zero", "negative"), ("positive",), 0)
            if enrolled and defined == 0:
                items.append(_unavailable("profitability", "no resampled path produced a defined return"))
            else:
                item = _share_item("profitability", "resampled profit", profitability, numerator=positive, enrolled=enrolled)
                if item is not None:
                    items.append(item)
        elif enrolled:
            items.append(_unavailable("profitability", _reason_of(profitability)))

    # The carry-over is required to exist for enrolled studies, but its rank never decides weakness: a low rank
    # is not proof that bad luck is ruled out, and a high rank is not the model proving itself right. A
    # reproduction with no defined path is not a carry-over at all.
    carry = chance.get("carry_over") or {}
    if enrolled:
        if carry.get("status") != "measured":
            items.append(_unavailable("carry-over", _reason_of(carry) if carry else "nothing was measured"))
        elif int(carry.get("finite") or 0) == 0:
            items.append(_unavailable("carry-over", "its reproduction produced no defined rehearsal path"))

    return items
