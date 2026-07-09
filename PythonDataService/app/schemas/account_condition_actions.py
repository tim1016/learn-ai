"""Closed cure-action vocabulary shared by account and bot lifecycle schemas."""

from __future__ import annotations

from typing import Literal

AccountCureAction = Literal[
    "resolve_exposure",
    "clear_freeze",
    "reconcile_now",
    "prove_evidence",
    "retire_replace",
]

__all__ = ["AccountCureAction"]
