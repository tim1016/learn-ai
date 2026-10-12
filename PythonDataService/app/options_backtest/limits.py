"""The limit of a range, kept where the setup routes can read it without importing the run service (#3030)."""

from __future__ import annotations

# The most sessions one request may name: about five years of trading days. Nothing older than the options plan can be
# tested anyway; this only keeps a mistyped range from asking the calendar for a century.
MAX_RANGE_SESSIONS = 1_300
