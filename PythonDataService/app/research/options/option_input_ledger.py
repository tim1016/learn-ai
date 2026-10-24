"""What the provider supplied, and failed to supply, to one ticker's IV history (#3524; PRD #3490 R25, T18).

The cross-sectional study's stock bars come from the data lake. Its option inputs do not: expiries,
contracts, daily option bars and the risk-free rate are asked of the provider while the IV history is
built, and the lake holds nothing that stands in for them. Those inputs must be named, not folded into a
"lake" label, and a request the provider did not answer must be visible rather than quietly becoming a
missing bar. This ledger is that record, one per ticker:

* **Attempts and failures per input**, each failure typed (stage, key, error class, plain reason). Before
  this ledger an option-bar fetch that failed was logged at debug and read as "no bar for that contract",
  indistinguishable from a contract that did not trade.
* **An observation digest per input**: sha256 over the (key, digest-of-what-was-used) pairs the provider
  answered with. It identifies what *this run* saw. The provider does not archive its answers, so the digest
  cannot reproduce them (``reproducible: false``); a later run that sees different answers shows a different
  digest. That is the retained identity R25 asks for where the input itself cannot be retained.
* **The risk-free rate's source** per trading day: FRED, or the one default when FRED could not answer. A default
  taken while a FRED key is configured is a FRED outage and is counted apart from a deployment that has no key: only
  the outage could answer differently on a second attempt.

The ledger records; it judges nothing. The runner decides what a failure means for a ticker.

Thread model: one ledger belongs to one ticker's build and is written from the building thread only (the
option-bar prefetch pool hands its results back to that thread before they are recorded).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app.research.runs.hashing import hash_payload
from app.services.fred_service import RATE_SOURCE_DEFAULT, RATE_SOURCE_FRED

Stage = Literal["option_expiries", "option_contracts", "option_bars"]

STAGES: tuple[Stage, ...] = ("option_expiries", "option_contracts", "option_bars")

#: Failures named in a payload; the total is always reported beside them.
MAX_LISTED_FAILURES = 25

#: How the provider is asked, per input. Static text: a result's data-source block names each source from here.
PROVIDER_INPUTS: Mapping[Stage, Mapping[str, str]] = {
    "option_expiries": {
        "provider": "polygon",
        "endpoint": "/v3/reference/options/contracts",
        "query": "as_of the first session of each month, and again as of a later session that finds no near or no far expiry in it: calls expiring from 7 days after that session to 45 days after the month's last, strike within 5% of that day's close",
        "selection": "for each session, the latest expiry 7 to 30 days out and the earliest 31 to 45 days out",
    },
    "option_contracts": {
        "provider": "polygon",
        "endpoint": "/v3/reference/options/contracts",
        "query": "one expiry (expired included), strike within 15% of the close on the first day that expiry is seen",
        "selection": "ATM call and put, and the 25-delta OTM put and call; reused for the whole expiry",
    },
    "option_bars": {
        "provider": "polygon",
        "endpoint": "/v2/aggs/ticker/{option}/range/1/day",
        "query": "daily aggregates over the study window, adjusted",
        "selection": "bid/ask mid on a tight spread, else vendor VWAP, else a close with volume of at least 50",
    },
}

_API_KEY = re.compile(r"(?i)(api[_-]?key=)[^&\s'\"]+")


def _scrub(text: str) -> str:
    """A vendor error's text with any API key in a URL blanked: a failure reason is shown to people and stored."""
    return _API_KEY.sub(r"\1REDACTED", text)[:300]


@dataclass(frozen=True)
class OptionInputFailure:
    """One provider request that did not answer, and why."""

    stage: Stage
    key: str
    error_type: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"stage": self.stage, "key": self.key, "error_type": self.error_type, "reason": self.reason}


class OptionInputLedger:
    """One ticker's provider-sourced option inputs: attempts, failures, and the identity of what was answered."""

    def __init__(self) -> None:
        self._attempts: Counter[str] = Counter()
        self._failures: list[OptionInputFailure] = []
        self._answers: dict[str, dict[str, str]] = {stage: {} for stage in STAGES}
        self._rate_days: Counter[str] = Counter()
        self._rate_fetch_failed_days = 0

    def answered(self, stage: Stage, key: str, used: Any) -> None:
        """The provider answered ``key``; ``used`` is the part of the answer the study reads (JSON-serializable)."""
        self._attempts[stage] += 1
        self._answers[stage][key] = hash_payload(used)

    def failed(self, stage: Stage, key: str, error: Exception) -> None:
        """The provider did not answer ``key``: the request raised ``error``."""
        self._attempts[stage] += 1
        self._failures.append(OptionInputFailure(stage, key, type(error).__name__, _scrub(str(error))))

    def risk_free_rate(self, source: str, *, fetch_failed: bool = False) -> None:
        """One trading day's rate was resolved from ``source`` (``RATE_SOURCE_FRED`` or ``RATE_SOURCE_DEFAULT``).

        ``fetch_failed`` marks a default taken because FRED, with a key configured, gave no rate.
        """
        self._rate_days[source] += 1
        if fetch_failed:
            self._rate_fetch_failed_days += 1

    @property
    def degraded(self) -> bool:
        """True when a provider request went unanswered or a rate fell back because FRED did not answer."""
        return bool(self._failures) or self._rate_fetch_failed_days > 0

    @property
    def failures(self) -> tuple[OptionInputFailure, ...]:
        return tuple(self._failures)

    @property
    def failure_count(self) -> int:
        return len(self._failures)

    def attempted(self, stage: Stage) -> int:
        return self._attempts[stage]

    def failed_count(self, stage: Stage) -> int:
        return sum(1 for failure in self._failures if failure.stage == stage)

    def rate_days(self, source: str) -> int:
        return self._rate_days[source]

    @property
    def rate_fetch_failed_days(self) -> int:
        """Days whose rate is the default only because FRED did not answer; asking again could give FRED's rate."""
        return self._rate_fetch_failed_days

    def observation_digest(self, stage: Stage) -> str | None:
        """sha256 over what the provider answered for ``stage``; ``None`` when it answered nothing."""
        answers = self._answers[stage]
        return hash_payload(sorted(answers.items())) if answers else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "inputs": {
                stage: {
                    "kind": "provider",
                    **PROVIDER_INPUTS[stage],
                    "attempted": self.attempted(stage),
                    "failed": self.failed_count(stage),
                    "observation_digest": self.observation_digest(stage),
                    "reproducible": False,
                }
                for stage in STAGES
            },
            "risk_free_rate": {
                "kind": "provider",
                "provider": "fred",
                "days_from_fred": self.rate_days(RATE_SOURCE_FRED),
                "days_from_default": self.rate_days(RATE_SOURCE_DEFAULT),
                "days_fetch_failed": self.rate_fetch_failed_days,
                "reproducible": False,
            },
            "failures": [failure.as_dict() for failure in self._failures[:MAX_LISTED_FAILURES]],
            "failures_total": len(self._failures),
            "degraded": self.degraded,
        }


def combine(ledgers: Mapping[str, OptionInputLedger]) -> dict[str, Any]:
    """The run's option-input sources: each input named once, its counts summed over the tickers that built an IV history.

    The observation digest of an input is the digest of its per-ticker digests, so it changes when any
    ticker's provider answers change. Failures carry their ticker.
    """
    ordered = sorted(ledgers.items())
    inputs: dict[str, Any] = {}
    for stage in STAGES:
        seen = [(ticker, digest) for ticker, ledger in ordered if (digest := ledger.observation_digest(stage)) is not None]
        inputs[stage] = {
            "kind": "provider",
            **PROVIDER_INPUTS[stage],
            "attempted": sum(ledger.attempted(stage) for _, ledger in ordered),
            "failed": sum(ledger.failed_count(stage) for _, ledger in ordered),
            "observation_digest": hash_payload(seen) if seen else None,
            "reproducible": False,
        }
    failures = [{"ticker": ticker, **failure.as_dict()} for ticker, ledger in ordered for failure in ledger.failures]
    return {
        "inputs": inputs,
        "risk_free_rate": {
            "kind": "provider",
            "provider": "fred",
            "days_from_fred": sum(ledger.rate_days(RATE_SOURCE_FRED) for _, ledger in ordered),
            "days_from_default": sum(ledger.rate_days(RATE_SOURCE_DEFAULT) for _, ledger in ordered),
            "days_fetch_failed": sum(ledger.rate_fetch_failed_days for _, ledger in ordered),
            "reproducible": False,
        },
        "failures": failures[:MAX_LISTED_FAILURES],
        "failures_total": len(failures),
        "degraded": any(ledger.degraded for _, ledger in ordered),
    }
