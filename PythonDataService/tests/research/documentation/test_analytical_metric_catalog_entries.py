"""Completeness and provenance tests for the LEAN catalog content shard."""

from __future__ import annotations

import json
from pathlib import Path

from app.research.documentation.analytical_metric_catalog_entries import (
    LEAN_NATIVE_CATALOG_VARIANTS,
    LEAN_ORACLE_FIXTURE,
    LEAN_ORACLE_TEST,
    LEAN_PORTFOLIO_ORACLE_KEYS,
    LEAN_PORTFOLIO_VARIANTS,
    LEAN_RUNTIME_SOURCE_KEYS,
    LEAN_RUNTIME_VARIANTS,
    LEAN_SOURCE_COMMIT,
    LEAN_TRADE_ORACLE_KEYS,
    LEAN_TRADE_VARIANTS,
    TRACER_OWNED_PORTFOLIO_KEYS,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
ORACLE_RESULT = (
    REPOSITORY_ROOT
    / "PythonDataService/tests/fixtures/golden/lean-statistics-oracle-v1/workspace/output/MyAlgorithm.json"
)


def _source_keys(variants: tuple[dict[str, object], ...]) -> set[str]:
    return {str(entry["aliases"][0]) for entry in variants}


def _oracle_key_sets() -> tuple[set[str], set[str]]:
    result = json.loads(ORACLE_RESULT.read_text(encoding="utf-8"))
    total_performance = result["totalPerformance"]
    return (
        set(total_performance["portfolioStatistics"]),
        set(total_performance["tradeStatistics"]),
    )


def test_lean_catalog_portfolio_keys_match_pinned_oracle_except_tracer_owned_sharpe() -> None:
    oracle_portfolio, _ = _oracle_key_sets()

    assert oracle_portfolio == set(LEAN_PORTFOLIO_ORACLE_KEYS)
    assert _source_keys(LEAN_PORTFOLIO_VARIANTS) == oracle_portfolio - set(TRACER_OWNED_PORTFOLIO_KEYS)
    assert len({entry["variant_id"] for entry in LEAN_PORTFOLIO_VARIANTS}) == len(LEAN_PORTFOLIO_VARIANTS)


def test_lean_catalog_trade_keys_match_pinned_oracle_exactly_once() -> None:
    _, oracle_trade = _oracle_key_sets()

    assert oracle_trade == set(LEAN_TRADE_ORACLE_KEYS)
    assert _source_keys(LEAN_TRADE_VARIANTS) == oracle_trade
    assert len({entry["variant_id"] for entry in LEAN_TRADE_VARIANTS}) == len(LEAN_TRADE_VARIANTS)


def test_lean_catalog_runtime_entries_remain_separate_from_66_value_parity_inventory() -> None:
    runtime_source_keys = _source_keys(LEAN_RUNTIME_VARIANTS)

    assert runtime_source_keys == set(LEAN_RUNTIME_SOURCE_KEYS)
    assert len(LEAN_PORTFOLIO_ORACLE_KEYS) + len(LEAN_TRADE_ORACLE_KEYS) == 66
    assert len(LEAN_RUNTIME_VARIANTS) == 9
    assert not runtime_source_keys & (set(LEAN_PORTFOLIO_ORACLE_KEYS) | set(LEAN_TRADE_ORACLE_KEYS))


def test_all_native_entries_carry_pinned_provenance_and_existing_evidence_receipts() -> None:
    fixture_path = REPOSITORY_ROOT / LEAN_ORACLE_FIXTURE
    test_path, _ = LEAN_ORACLE_TEST.split("::", maxsplit=1)

    assert fixture_path.is_dir()
    assert (REPOSITORY_ROOT / test_path).is_file()
    manifest = json.loads((fixture_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["lean_source_commit"] == LEAN_SOURCE_COMMIT

    for entry in LEAN_NATIVE_CATALOG_VARIANTS:
        assert LEAN_SOURCE_COMMIT in str(entry["source_reference"])
        assert entry["canonical_symbol"]
        assert entry["validating_tests"]
        assert entry["fixture_or_receipt"] == LEAN_ORACLE_FIXTURE
        assert entry["value_states"]

    for entry in (*LEAN_PORTFOLIO_VARIANTS, *LEAN_TRADE_VARIANTS):
        assert entry["numerical_tolerance"]
        assert entry["formula_latex"]

    for entry in LEAN_RUNTIME_VARIANTS:
        assert entry["formula_latex"] is None
        assert entry["numerical_tolerance"] is None
        assert "no local formula contract" in " ".join(entry["common_misreadings"])


def test_lean_trade_profit_factor_keeps_the_finite_no_loss_sentinel_distinct() -> None:
    profit_factor = next(entry for entry in LEAN_TRADE_VARIANTS if entry["aliases"][0] == "profitFactor")
    states = {state["state"]: state for state in profit_factor["value_states"]}

    assert states["sentinel"]["display"] == "10 when total profit is positive and there is no loss denominator."
    assert "finite" in states["sentinel"]["scoring_behavior"]
    assert "sentinel" in states["sentinel"]["scoring_behavior"]
    assert "not emitted" in states["infinite"]["display"]
    assert states["zero"]["display"] == "0 (a valid native numeric result)"
    assert "unavailable" in states["unavailable"]["display"].lower()


def test_declared_platform_alternatives_use_the_cross_slice_stable_ids() -> None:
    alternatives = {
        alternative
        for entry in LEAN_NATIVE_CATALOG_VARIANTS
        for alternative in entry["alternative_variant_ids"]
    }

    assert alternatives == {
        "maximum_drawdown.platform.v1",
        "profit_factor.platform.v1",
        "sortino.platform.v1",
    }
