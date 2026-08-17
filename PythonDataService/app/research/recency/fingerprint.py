"""Canonical evidence fingerprint for one recency trade.

Formula: sha256 of the canonical JSON over every dimension that can make
two executions materially different — symbol, strategy, strategy-code
revision, parameter assignment, data policy, fill model, commissions, and
the trade's own entry/exit instants. Parameters alone are insufficient:
two runs sharing a params_hash but differing in, say, the fill model must
never collapse to one identity (design spec D16 — the direct fix for the
code-review finding that params-only dedup destroys scientific
provenance).
Reference: PRD https://github.com/tim1016/learn-ai/issues/1577; design spec
docs/superpowers/specs/2026-08-16-recency-chart-design.md §3 (D16), §4.1.
Canonical implementation: this file.
Validated against: tests/research/recency/test_fingerprint.py.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class RunFingerprintBase:
    """Every fingerprint component shared by all of one run's trades."""

    symbol: str
    strategy_key: str
    strategy_code_version: str
    params_hash: str
    data_policy: str
    fill_mode: str
    commission_per_order: float


def trade_fingerprint(base: RunFingerprintBase, entry_ms: int, exit_ms: int) -> str:
    """Canonical identity for one trade: the run's evidence base plus its exact window."""
    canonical = json.dumps(
        {
            "symbol": base.symbol,
            "strategy_key": base.strategy_key,
            "strategy_code_version": base.strategy_code_version,
            "params_hash": base.params_hash,
            "data_policy": base.data_policy,
            "fill_mode": base.fill_mode,
            "commission_per_order": base.commission_per_order,
            "entry_ms": entry_ms,
            "exit_ms": exit_ms,
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
