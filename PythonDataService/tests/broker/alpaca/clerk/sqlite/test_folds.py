"""Unit coverage for the Clerk fold registry's closed vocabulary."""

from __future__ import annotations

import sqlite3

import pytest

from app.broker.alpaca.clerk.sqlite.folds import FoldRegistry, UnregisteredTransitionKind


def test_fold_registry_rejects_an_unregistered_transition_kind() -> None:
    registry = FoldRegistry()
    conn = sqlite3.connect(":memory:")

    with pytest.raises(UnregisteredTransitionKind, match="UNRECOGNIZED"):
        registry.apply(conn, {"transition_kind": "UNRECOGNIZED"})
