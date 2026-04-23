"""Contract tests for app.research.documentation.formulas.

The Angular UI depends on every feature/validation entry exposing a stable
shape (name, formula_latex, interpretation). Tests guard that contract so a
silent key rename doesn't break the lab UI.
"""

from __future__ import annotations

import pytest

from app.research.documentation.formulas import (
    FEATURE_DOCUMENTATION,
    TARGET_DOCUMENTATION,
    VALIDATION_DOCUMENTATION,
    get_all_documentation,
)

REQUIRED_FEATURE_KEYS = {"name", "formula_latex", "variables", "example", "interpretation", "implementation"}
REQUIRED_VALIDATION_KEYS = {"name", "formula_latex", "variables", "interpretation"}
REQUIRED_EXAMPLE_KEYS = {"inputs", "calculation", "result"}


@pytest.mark.parametrize("feature_key", list(FEATURE_DOCUMENTATION.keys()))
def test_feature_documentation_has_required_keys(feature_key: str):
    entry = FEATURE_DOCUMENTATION[feature_key]

    assert REQUIRED_FEATURE_KEYS.issubset(entry.keys()), f"feature {feature_key} missing keys"
    assert REQUIRED_EXAMPLE_KEYS.issubset(entry["example"].keys())
    assert isinstance(entry["formula_latex"], str) and entry["formula_latex"].strip()
    assert isinstance(entry["variables"], dict) and entry["variables"]


@pytest.mark.parametrize("validation_key", list(VALIDATION_DOCUMENTATION.keys()))
def test_validation_documentation_has_required_keys(validation_key: str):
    entry = VALIDATION_DOCUMENTATION[validation_key]

    assert REQUIRED_VALIDATION_KEYS.issubset(entry.keys())
    assert isinstance(entry["formula_latex"], str) and entry["formula_latex"].strip()


def test_target_documentation_shape():
    assert TARGET_DOCUMENTATION["name"]
    assert TARGET_DOCUMENTATION["formula_latex"]
    assert REQUIRED_EXAMPLE_KEYS.issubset(TARGET_DOCUMENTATION["example"].keys())
    assert "constraints" in TARGET_DOCUMENTATION
    assert isinstance(TARGET_DOCUMENTATION["constraints"], list)


def test_get_all_documentation_bundles_three_sections():
    bundle = get_all_documentation()

    assert set(bundle.keys()) == {"target", "features", "validation"}
    assert bundle["target"] is TARGET_DOCUMENTATION
    assert bundle["features"] is FEATURE_DOCUMENTATION
    assert bundle["validation"] is VALIDATION_DOCUMENTATION


def test_feature_registry_covers_documented_keys():
    """Every module-documented feature should exist in the feature registry."""
    from app.research.features.registry import list_available_features

    available = set(list_available_features())
    documented = set(FEATURE_DOCUMENTATION.keys())

    missing_in_registry = documented - available
    assert not missing_in_registry, f"documented features not in registry: {missing_in_registry}"
