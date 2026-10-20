"""Canonical evidence fingerprint.

Two executions must collapse to the same trade identity only when they
agree on symbol, strategy, strategy-code revision, parameters, data
policy, fill model, commissions, AND the exact trade window — never on
parameters alone. This is the direct fix for the code-review finding
that params-only dedup destroys scientific provenance.
"""

from __future__ import annotations

import pytest

from app.research.recency.fingerprint import RunFingerprintBase, trade_fingerprint


def _base(**overrides: object) -> RunFingerprintBase:
    defaults = dict(
        symbol="SPY",
        strategy_key="ema_crossover_2_bps",
        strategy_code_version="abc123",
        params_hash="hash1",
        data_policy="polygon-adjusted-regular-minute",
        fill_mode="signal_bar_close",
        commission_per_order=0.0,
    )
    defaults.update(overrides)
    return RunFingerprintBase(**defaults)  # type: ignore[arg-type]


def test_the_fingerprint_follows_the_trade_window() -> None:
    a = trade_fingerprint(_base(), entry_ms=1000, exit_ms=2000)
    b = trade_fingerprint(_base(), entry_ms=1000, exit_ms=2000)
    c = trade_fingerprint(_base(), entry_ms=1500, exit_ms=2000)
    assert a == b, "identical evidence produces identical fingerprint"
    assert a != c, "different entry time changes the fingerprint"


@pytest.mark.parametrize(
    ("field", "value_a", "value_b"),
    [
        ("strategy_code_version", "abc123", "def456"),
        ("data_policy", "polygon-adjusted-regular-minute", "polygon-unadjusted-regular-minute"),
        ("commission_per_order", 0.0, 1.0),
        ("symbol", "SPY", "QQQ"),
    ],
    ids=[
        "different_strategy_code_version_changes_the_fingerprint",
        "different_data_policy_changes_the_fingerprint",
        "different_commission_changes_the_fingerprint",
        "same_params_but_different_symbol_does_not_collapse",
    ],
)
def test_a_differing_base_field_changes_the_fingerprint(field: str, value_a: object, value_b: object) -> None:
    a = trade_fingerprint(_base(**{field: value_a}), entry_ms=1000, exit_ms=2000)
    b = trade_fingerprint(_base(**{field: value_b}), entry_ms=1000, exit_ms=2000)
    assert a != b
