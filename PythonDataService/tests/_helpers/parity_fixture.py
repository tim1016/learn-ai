"""Shared parity-fixture pin for LEAN vs engine receipt tests.

When rotating the parity fixture, change ``PARITY_FIXTURE_NAME`` in the
same PR that adds the new fixture; old fixtures may remain on disk
during the transition. The pin keeps both the integration parity test
and the live-Polygon freshness canary aimed at exactly one window so
unrelated fixture directories do not break either.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PARITY_FIXTURE_NAME = "spy_minute_2025-01-13_2025-01-17"

_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "polygon_capture"


def parity_fixture_dir() -> Path:
    """Return the pinned parity-fixture directory or skip the test.

    Skips rather than fails when the fixture is absent so the suite
    stays green on fresh clones / sparse checkouts that have not yet
    materialized the fixture tree.
    """
    fixture_dir = _FIXTURE_ROOT / PARITY_FIXTURE_NAME
    if not fixture_dir.exists():
        pytest.skip(f"parity fixture {PARITY_FIXTURE_NAME!r} not present under {_FIXTURE_ROOT}")
    if not (fixture_dir / "metadata.json").exists():
        pytest.skip(f"parity fixture {PARITY_FIXTURE_NAME!r} is missing metadata.json")
    return fixture_dir
