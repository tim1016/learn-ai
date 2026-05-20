"""BarsSpec equality and serialization contract.

The v1 PR B equivalence gate does NOT normalize timespan/multiplier pairs.
{minute, 60} and {hour, 1} are NOT equal even though they describe the
same bar length. Flipping this contract requires changing this test
deliberately. See docs/superpowers/specs/2026-05-19-pr-b-engine-lab-unified-design.md § 4.3.
"""

from __future__ import annotations

import json
from dataclasses import asdict


def test_bars_spec_equality_is_field_level() -> None:
    from app.lean_sidecar.data_policy import BarsSpec

    assert BarsSpec(timespan="minute", multiplier=15) == BarsSpec(timespan="minute", multiplier=15)


def test_bars_spec_minute_60_not_equal_to_hour_1() -> None:
    """V1 contract pin: no semantic normalization. Different (timespan, multiplier) → not equal."""
    from app.lean_sidecar.data_policy import BarsSpec

    assert BarsSpec(timespan="minute", multiplier=60) != BarsSpec(timespan="hour", multiplier=1)


def test_bars_spec_json_shape() -> None:
    from app.lean_sidecar.data_policy import BarsSpec

    serialized = json.dumps(asdict(BarsSpec(timespan="minute", multiplier=15)))
    parsed = json.loads(serialized)
    assert parsed == {"timespan": "minute", "multiplier": 15}
