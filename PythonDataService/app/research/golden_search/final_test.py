"""The final test's record: which fits sit it, and how a stored final test reads back (#2930, ADR 0074 decision 4).

A final test runs the frozen incumbent and every fit — the all-period fit
and the recent fit — whose settings differ from the incumbent's. Two fits
that chose the same settings are one entry, named by the first and listing
the other in ``same_as``. Each fit is judged on its own; the owner names the
fit to approve after the outcomes are recorded.

A final test recorded before both fits sat it holds one candidate, locked
before the test opened. :func:`recorded_fits` reads it as one fit, so every
reader sees one shape.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from app.research.golden_search.evidence import CANDIDATE_LABELS

#: When the owner named the fit: before the test opened (a legacy record), or after its outcomes were recorded.
Pick = Literal["before_test", "after_test"]

FIT_KEYS: tuple[str, ...] = ("all_period", "recent")


def fits_to_test(candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The unscored entry of each fit that sits the final test, from the stored evidence candidates.

    A fit with the incumbent's settings is left out: the incumbent runs
    anyway, and testing the current settings against themselves proves nothing.
    """
    by_key = {str(item["key"]): item for item in candidates}
    incumbent = by_key.get("incumbent")
    incumbent_hash = None if incumbent is None else incumbent["point_hash"]
    fits: list[dict[str, Any]] = []
    for key in FIT_KEYS:
        item = by_key.get(key)
        if item is None or item["point_hash"] == incumbent_hash:
            continue
        twin = next((fit for fit in fits if fit["point_hash"] == item["point_hash"]), None)
        if twin is not None:
            twin["same_as"].append(key)
            continue
        fits.append(
            {
                "key": key,
                "same_as": [],
                "point": dict(item["point"]),
                "point_hash": item["point_hash"],
                "outcome": None,
                "checks": [],
                "retention": None,
                "metrics": None,
            }
        )
    return fits


def pick_of(exam: Mapping[str, Any]) -> Pick:
    return "before_test" if "candidate_key" in exam else "after_test"


def recorded_fits(exam: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every fit a stored final test holds; a record with one locked candidate reads as that one fit."""
    if "fits" in exam:
        return [dict(fit) for fit in exam["fits"]]
    return [
        {
            "key": exam["candidate_key"],
            "same_as": [],
            "point": dict(exam["candidate_point"]),
            "point_hash": exam["candidate_point_hash"],
            "outcome": exam.get("outcome"),
            "checks": list(exam.get("checks", [])),
            "retention": exam.get("retention"),
            "metrics": exam.get("candidate_metrics"),
        }
    ]


def fit_for(exam: Mapping[str, Any], candidate_key: str | None) -> dict[str, Any] | None:
    """The tested fit ``candidate_key`` names, by its own key or as a fit with the same settings."""
    if candidate_key is None:
        return None
    return next((fit for fit in recorded_fits(exam) if candidate_key == fit["key"] or candidate_key in fit["same_as"]), None)


def fit_label(fit: Mapping[str, Any]) -> str:
    """``All-period fit``, ``Recent fit``, or ``All-period and recent fit`` when both chose the same settings."""
    names = [CANDIDATE_LABELS[key] for key in (fit["key"], *fit["same_as"])]
    return " and ".join([names[0].removesuffix(" fit"), *(name.lower() for name in names[1:])]) if len(names) > 1 else names[0]

