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
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "polygon_capture"


def _pick_fixture() -> Path:
    if not FIXTURE_ROOT.exists():
        pytest.skip(f"no fixture directory at {FIXTURE_ROOT}")
    candidates = sorted(d for d in FIXTURE_ROOT.iterdir() if d.is_dir() and (d / "metadata.json").exists())
    if not candidates:
        pytest.skip(f"no Polygon fixture committed under {FIXTURE_ROOT}")
    if len(candidates) > 1:
        raise RuntimeError("freshness canary expects exactly one fixture")
    return candidates[0]


@pytest.mark.slow
@pytest.mark.skipif(
    not os.environ.get("POLYGON_API_KEY"),
    reason="POLYGON_API_KEY unset; freshness canary needs live Polygon access",
)
def test_polygon_fixture_matches_live_refetch() -> None:
    from app.lean_sidecar.polygon_canonical import PolygonProvider
    from app.services.polygon_client import PolygonClientService

    fixture_dir = _pick_fixture()
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
