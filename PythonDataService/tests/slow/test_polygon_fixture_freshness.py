"""Canary: live Polygon ≡ committed fixture.

Re-fetches the parity fixture's window from live Polygon and asserts
byte-equivalence against the committed bars.json. Catches the case
where Polygon amends historical bars (rare but observed); a failure
here means the fixture needs regeneration with a justification commit.

@pytest.mark.slow — opt in via ``pytest -m slow``.
Requires POLYGON_API_KEY.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date

import pytest

from tests._helpers.parity_fixture import parity_fixture_dir


@pytest.mark.slow
@pytest.mark.skipif(
    not os.environ.get("POLYGON_API_KEY"),
    reason="POLYGON_API_KEY unset; freshness canary needs live Polygon access",
)
def test_polygon_fixture_matches_live_refetch() -> None:
    from app.lean_sidecar.polygon_canonical import PolygonProvider
    from app.services.polygon_client import PolygonClientService

    fixture_dir = parity_fixture_dir()
    meta = json.loads((fixture_dir / "metadata.json").read_text())

    provider = PolygonProvider(polygon=PolygonClientService())
    live = provider.fetch_minute_bars(
        symbol=meta["symbol"],
        start_date=date.fromisoformat(meta["from_date"]),
        end_date=date.fromisoformat(meta["to_date"]),
        adjusted=meta["adjusted"],
    )

    live_json = json.dumps(live, separators=(",", ":"))
    live_sha = hashlib.sha256(live_json.encode("utf-8")).hexdigest()

    assert live_sha == meta["bars_sha256"], (
        f"Polygon refetch sha256 ({live_sha[:12]}...) differs from fixture "
        f"({meta['bars_sha256'][:12]}...). Polygon may have amended the data. "
        f"Regenerate the fixture with scripts/regenerate_polygon_fixture.py "
        f"and explain in the commit message."
    )
