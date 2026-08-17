"""Canonical evidence fingerprint (design spec D16).

Two executions must collapse to the same trade identity only when they
agree on symbol, strategy, strategy-code revision, parameters, data
policy, fill model, commissions, AND the exact trade window — never on
parameters alone. This is the direct fix for the code-review finding
that params-only dedup destroys scientific provenance.
"""

from __future__ import annotations

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


def test_identical_evidence_produces_identical_fingerprint() -> None:
    a = trade_fingerprint(_base(), entry_ms=1000, exit_ms=2000)
    b = trade_fingerprint(_base(), entry_ms=1000, exit_ms=2000)
    assert a == b


def test_different_entry_time_changes_the_fingerprint() -> None:
    a = trade_fingerprint(_base(), entry_ms=1000, exit_ms=2000)
    b = trade_fingerprint(_base(), entry_ms=1500, exit_ms=2000)
    assert a != b


def test_different_strategy_code_version_changes_the_fingerprint() -> None:
    a = trade_fingerprint(_base(strategy_code_version="abc123"), entry_ms=1000, exit_ms=2000)
    b = trade_fingerprint(_base(strategy_code_version="def456"), entry_ms=1000, exit_ms=2000)
    assert a != b


def test_different_data_policy_changes_the_fingerprint() -> None:
    a = trade_fingerprint(_base(data_policy="polygon-adjusted-regular-minute"), entry_ms=1000, exit_ms=2000)
    b = trade_fingerprint(_base(data_policy="polygon-unadjusted-regular-minute"), entry_ms=1000, exit_ms=2000)
    assert a != b


def test_different_commission_changes_the_fingerprint() -> None:
    a = trade_fingerprint(_base(commission_per_order=0.0), entry_ms=1000, exit_ms=2000)
    b = trade_fingerprint(_base(commission_per_order=1.0), entry_ms=1000, exit_ms=2000)
    assert a != b


def test_same_params_but_different_symbol_does_not_collapse() -> None:
    a = trade_fingerprint(_base(symbol="SPY"), entry_ms=1000, exit_ms=2000)
    b = trade_fingerprint(_base(symbol="QQQ"), entry_ms=1000, exit_ms=2000)
    assert a != b
