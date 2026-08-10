"""Integrity checks for the deterministic Strategy Lab manual example."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.research.documentation.analytical_metric_catalog import catalog

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIRECTORY = REPOSITORY_ROOT / "PythonDataService/tests/fixtures/golden/strategy-lab-analytical-manual-v1"


def _fixture() -> dict[str, Any]:
    return json.loads((FIXTURE_DIRECTORY / "fixture.json").read_text(encoding="utf-8"))


def _assert_int64_ms_timestamps(value: Any, key: str = "") -> None:
    if isinstance(value, dict):
        for nested_key, nested_value in value.items():
            _assert_int64_ms_timestamps(nested_value, nested_key)
    elif isinstance(value, list):
        for item in value:
            _assert_int64_ms_timestamps(item, key)
    elif key.endswith("_ms"):
        assert isinstance(value, int) and not isinstance(value, bool)


def _canonical_source_path(symbol: str) -> Path:
    module_or_path = symbol.split("::", maxsplit=1)[0]
    if module_or_path.startswith("app."):
        parts = module_or_path.split(".")
        for end in range(len(parts), 0, -1):
            candidate = REPOSITORY_ROOT / "PythonDataService" / Path(*parts[:end]).with_suffix(".py")
            if candidate.is_file():
                return candidate
        return REPOSITORY_ROOT / "PythonDataService" / Path(*parts).with_suffix(".py")
    return REPOSITORY_ROOT / module_or_path


def test_manual_fixture_is_hashed_complete_and_uses_only_int64_ms_timestamps() -> None:
    fixture = _fixture()
    manifest = json.loads((FIXTURE_DIRECTORY / "manifest.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256((FIXTURE_DIRECTORY / "fixture.json").read_bytes()).hexdigest()
    run = fixture["run"]

    assert fixture["fixture_id"] == manifest["fixture_id"] == "strategy-lab-analytical-manual-v1"
    assert manifest["files"]["fixture.json"]["sha256"] == digest
    assert run["full_run"]["completed_trades"] == 867
    assert run["full_run"]["recent_trade_ledger_rows"] == 500
    assert len(run["verdict"]["required_input_keys"]) == 17
    assert len(set(run["verdict"]["required_input_keys"])) == 17
    assert run["verdict"]["status"] == "complete"
    assert fixture["focused_states"]["platform_sortino_unavailable"]["state"] == "unavailable"
    assert fixture["focused_states"]["platform_zero"]["state"] == "zero"
    assert fixture["focused_states"]["lean_profit_factor_no_loss"]["state"] == "sentinel"
    assert fixture["focused_states"]["legacy_verdict"]["status"] is None
    _assert_int64_ms_timestamps(fixture)


def test_catalog_references_resolve_and_fixture_contexts_exist_in_the_generated_catalog() -> None:
    variants = {variant.variant_id: variant for variant in catalog().variants}

    for variant in variants.values():
        assert variant.contract_id, variant.variant_id
        assert _canonical_source_path(variant.canonical_symbol).is_file(), variant.canonical_symbol
        for test_receipt in variant.validating_tests:
            assert (REPOSITORY_ROOT / test_receipt.split("::", maxsplit=1)[0]).is_file(), test_receipt
        if variant.fixture_or_receipt:
            assert (REPOSITORY_ROOT / variant.fixture_or_receipt).exists(), variant.fixture_or_receipt

    for context in _fixture()["run"]["metric_documentation"]:
        variant = variants[context["variant_id"]]
        assert variant.metric_id == context["metric_id"]
        assert variant.producer == context["producer"]
        assert variant.contract_id == context["contract_id"]
