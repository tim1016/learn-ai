"""Shared closed vocabularies for durable bot-lifecycle records and views."""

from __future__ import annotations

from typing import Literal

BotDutyOutcomeKind = Literal[
    "CLOCKED_OUT_FLAT",
    "STOPPED",
    "HALTED",
    "CRASHED",
    "FAILED_LAUNCH",
    "EXITED_UNVERIFIED",
    "RETIRED",
]


UNCLEAN_DUTY_OUTCOMES = frozenset({"CRASHED", "EXITED_UNVERIFIED"})

__all__ = ["UNCLEAN_DUTY_OUTCOMES", "BotDutyOutcomeKind"]
