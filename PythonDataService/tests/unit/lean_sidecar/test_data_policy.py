"""DataPolicy contract tests."""

from __future__ import annotations

import json
import warnings


def test_data_policy_canonical_import_path() -> None:
    """DataPolicy is importable from app.lean_sidecar.data_policy."""
    from app.lean_sidecar.data_policy import BarsSpec, DataPolicy  # noqa: F401


def test_data_policy_manifest_alias_emits_deprecation_warning() -> None:
    """DataPolicyManifest alias still works for one cycle but warns."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        from app.lean_sidecar.data_policy import DataPolicyManifest  # noqa: F401

    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) >= 1
    assert "DataPolicy" in str(deprecations[0].message)


def test_data_policy_roundtrips_to_json_with_sorted_keys() -> None:
    """Canonical serialization is sort_keys=True; roundtrip preserves values."""
    from dataclasses import asdict

    from app.lean_sidecar.data_policy import BarsSpec, DataPolicy

    dp = DataPolicy(
        source="polygon",
        symbol="SPY",
        adjusted=True,
        session="regular",
        input_bars=BarsSpec(timespan="minute", multiplier=1),
        strategy_bars=BarsSpec(timespan="minute", multiplier=15),
        timestamp_policy="bar_close_ms_utc",
        timezone="America/New_York",
        provider_kind="live",
        fixture_id=None,
        fixture_sha256=None,
    )
    serialized = json.dumps(asdict(dp), sort_keys=True)
    parsed = json.loads(serialized)
    assert parsed["symbol"] == "SPY"
    assert parsed["input_bars"]["multiplier"] == 1
    assert parsed["strategy_bars"]["multiplier"] == 15
    assert parsed["adjusted"] is True
