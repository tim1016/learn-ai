"""The startup-join settle time covers every history revision IBKR was seen to make (#2410).

Receipt: ``tests/fixtures/golden/ibkr-history-settle-2026-09-24`` (see its
``attribution.md``). IBKR serves a just-closed minute's row at once, so a row's
presence proves nothing; the run asks only after ``STARTUP_JOIN_SETTLE_MS``.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import Settings

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "golden" / "ibkr-history-settle-2026-09-24" / "probe.jsonl"


def _records() -> list[dict[str, object]]:
    return [json.loads(line) for line in _FIXTURE.read_text().splitlines() if line.strip()]


def test_the_fixture_is_the_sample_the_reference_note_cites() -> None:
    records = _records()

    assert len(records) == 16
    assert sum(bool(record["changed_after_close"]) for record in records) == 3


def test_the_default_settle_time_is_at_least_twice_the_slowest_revision_observed() -> None:
    slowest_settle_ms = max(float(record["first_final_s"]) for record in _records()) * 1_000
    default_settle_ms = Settings.model_fields["STARTUP_JOIN_SETTLE_MS"].default

    assert slowest_settle_ms == 1_320
    assert default_settle_ms >= 2 * slowest_settle_ms
