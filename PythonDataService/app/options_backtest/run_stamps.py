"""What a run was made under, stamped on it when it is made (#3108, PRD #3095).

Formula: none. A stamp names the version of the replay's rules and the requirement rule that held the collateral.
Reference: ADR 0078 decision 11. The requirement rule is Alpaca's published rule for covered positions, "Options
  Level 3 Trading", https://docs.alpaca.markets/docs/options-level-3-trading, the one ``app/options/requirement.py``
  works out and ``cover_book.collateral`` asks. Its page was read on 2026-10-12.
Canonical implementation: this file.
Validated against: tests/options_backtest/test_run_capital.py.

The rule is today's. A run replays past sessions under the rule as it stood when its page was read, so a stamp is no
evidence of what a broker required on a session's date, and a result says so.

``REPLAY_RULES_VERSION`` is raised whenever a change to the replay's rules can change a figure of a run made under
the same setup and the same data. A run stored before stamps existed has none: it reads "not recorded".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.utils.session_anchors import et_midnight_ms

# 1: bought legs and covered sold legs, by the clock (ADR 0078 as amended by #3104, #3105, #3106 and #3108).
# 2: a sold leg has its own stop, a stop on a bought leg closes the sold legs it protects with it, and a sold leg
#    re-enters (ADR 0078 as amended by #3107). A setup that sells and carries a stop or a re-entry rule was refused
#    under 1, so no run made under 1 would have another figure; the version says which rules a result was made under,
#    and a setup that sells with stops now trades them, with closes (and so cash, collateral and capital evidence)
#    that version 1 never made.
REPLAY_RULES_VERSION = 2
# The name of the requirement rule, as a result says it.
REQUIREMENT_RULE = "alpaca_covered_positions"
# New York midnight of the day the rule's page was read, as every date-anchored value is an instant.
REQUIREMENT_RULE_READ_AT_MS = et_midnight_ms(date(2026, 10, 12))


@dataclass(frozen=True)
class RunStamps:
    """The rules a run was made under."""

    replay_rules_version: int
    requirement_rule: str
    # New York midnight of the day the requirement rule's page was read, in int64 ms UTC.
    requirement_rule_read_at_ms: int


def current_stamps() -> RunStamps:
    """The stamps a run made now carries."""
    return RunStamps(REPLAY_RULES_VERSION, REQUIREMENT_RULE, REQUIREMENT_RULE_READ_AT_MS)
