"""Which requested members are in a multi-member study's statistics, and why each other one is not (#3524; PRD #3490 T18).

A study over a universe never silently shrinks it. Every requested member is accounted for exactly once: in the
statistics (``evaluated``), left out with a stage, a typed code and a plain reason (``excluded``), or in the
statistics but built on inputs a second attempt could have improved (``degraded``). The statistics divide by the
evaluated count, and ``complete`` is true only when nothing was left out and nothing was degraded.

This module knows nothing of where a member's data came from: the data lake's admission of its stock bars and of its
option inputs, and the math runner, each name their own exclusions with it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

#: Where a member can be left out of the statistics.
ExclusionStage = Literal["stock_bars", "option_inputs", "iv_inputs", "iv_diagnostics", "computation"]

#: The code a study ends with when no requested member reached its statistics.
NO_MEMBER_EVALUATED = "no_member_evaluated"

#: Members named in a payload; the total is always reported beside them.
MAX_LISTED_MEMBERS = 50

#: What "valid members only" means, in the words the result shows.
INCLUSION_POLICY = {
    "name": "valid_members_only",
    "description": (
        "Statistics (aggregate IC, N_eff, binomial test, stage) count only members whose per-ticker IC ran on usable "
        "stock bars and IV. Every other requested member is listed as excluded, with its reason."
    ),
}


@dataclass(frozen=True)
class MemberExclusion:
    """A member left out of the statistics, with where, why and whether asking again could answer differently."""

    ticker: str
    stage: ExclusionStage
    code: str
    reason: str
    retryable: bool
    refusal: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "stage": self.stage,
            "code": self.code,
            "reason": self.reason,
            "retryable": self.retryable,
            "refusal": self.refusal,
        }


def universe_record(
    requested: Sequence[str],
    *,
    evaluated: Sequence[str],
    excluded: Sequence[dict[str, Any]],
    degraded: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """The one universe block, for a finished study and for a refused or failed one alike."""
    return {
        "requested": list(requested),
        "requested_count": len(requested),
        "evaluated": list(evaluated),
        "evaluated_count": len(evaluated),
        "excluded": list(excluded[:MAX_LISTED_MEMBERS]),
        "excluded_count": len(excluded),
        "degraded": list(degraded[:MAX_LISTED_MEMBERS]),
        "denominator": len(evaluated),
        "inclusion_policy": INCLUSION_POLICY,
        "complete": len(evaluated) == len(requested) and not degraded,
    }


def universe_of_rows(requested: Sequence[str], rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Account for every requested member from the study's per-ticker rows.

    A row is evaluated when its ``validity`` is ``valid``; every other row must carry its exclusion. A valid row
    whose ``option_inputs`` are degraded (a rate that is the default only because FRED, the one live input, did not
    answer) stays in the statistics and is named in ``degraded`` with the count of days.
    """
    evaluated: list[str] = []
    excluded: list[dict[str, Any]] = []
    degraded: list[dict[str, Any]] = []
    for row in rows:
        if row["validity"] == "valid":
            evaluated.append(row["ticker"])
            inputs = row["option_inputs"]
            if inputs and inputs["degraded"]:
                degraded.append(
                    {"ticker": row["ticker"], "rate_days_fetch_failed": inputs["risk_free_rate"]["days_fetch_failed"]}
                )
        elif row["exclusion"] is None:
            raise RuntimeError(f"{row['ticker']} is {row['validity']} but carries no exclusion")
        else:
            excluded.append(row["exclusion"])
    return universe_record(requested, evaluated=evaluated, excluded=excluded, degraded=degraded)


def members_summary(members: Sequence[tuple[str, str]], *, shown: int = 3) -> str:
    """The first few ``(ticker, reason)`` pairs as ``TICKER: reason`` sentences, then how many more there are."""
    listed = " ".join(f"{ticker}: {reason}" for ticker, reason in members[:shown])
    more = f" And {len(members) - shown} more." if len(members) > shown else ""
    return f"{listed}{more}"
