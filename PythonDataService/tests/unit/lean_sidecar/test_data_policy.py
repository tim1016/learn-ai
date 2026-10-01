"""DataPolicy contract tests."""

from __future__ import annotations

import json


def test_data_policy_canonical_import_path() -> None:
    """DataPolicy is importable from app.lean_sidecar.data_policy."""
    from app.lean_sidecar.data_policy import BarsSpec, DataPolicy  # noqa: F401


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
