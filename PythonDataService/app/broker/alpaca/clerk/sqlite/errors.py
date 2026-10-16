"""Shared refusal boundary for the custody store and its mirror."""

from __future__ import annotations


class ClerkSqliteError(Exception):
    """Base for every fail-closed condition this repository can raise."""
