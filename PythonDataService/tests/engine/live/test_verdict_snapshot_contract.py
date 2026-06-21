"""PRD #619-A — ``verdict_snapshot.json`` shape stability contract.

Asserts the on-disk shape ``LiveEngine._write_verdict_snapshot``
produces does not drift across the ADR-0011 amendment in 619-A. The
file remains ``{verdict, observed_at_ms_utc}`` atomically written; the
Resume guard (``BrokerSafetyArtifact`` reader) consumes only those two
fields.

This is intentionally a separate test from the verdict-derivation
tests so a future shape change cannot slip through unnoticed alongside
a derivation refactor.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine.live.live_engine import LiveEngine


class _MinimalEngine:
    """Just enough of ``LiveEngine`` to exercise ``_write_verdict_snapshot``."""

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir


@pytest.mark.parametrize(
    "verdict_value",
    ["paper-only", "unsafe", "unknown", None],
)
def test_verdict_snapshot_carries_only_pinned_fields(
    tmp_path: Path, verdict_value: str | None
) -> None:
    """The snapshot is exactly ``{verdict, observed_at_ms_utc}``.

    PRD #619-A pins this shape — any expansion (carrying capability,
    posture, etc.) belongs in 619-B's ``engine_runtime.json``, not on
    top of this file.
    """
    engine = _MinimalEngine(tmp_path)

    LiveEngine._write_verdict_snapshot(engine, verdict_value)  # type: ignore[arg-type]

    path = tmp_path / "verdict_snapshot.json"
    assert path.exists()

    data = json.loads(path.read_text(encoding="utf-8"))

    # Field set is pinned: no extras.
    assert set(data.keys()) == {"verdict", "observed_at_ms_utc"}

    # verdict carries the raw value (None stays None; non-string is
    # coerced via ``str``).
    if verdict_value is None:
        assert data["verdict"] is None
    else:
        assert data["verdict"] == verdict_value

    # observed_at_ms_utc is an int64 ms epoch.
    assert isinstance(data["observed_at_ms_utc"], int)
    assert data["observed_at_ms_utc"] > 0


def test_verdict_snapshot_atomic_replace_leaves_no_tmp(tmp_path: Path) -> None:
    """``.tmp + rename`` semantics — after a successful write there is
    no ``verdict_snapshot.json.tmp`` left behind."""
    engine = _MinimalEngine(tmp_path)
    LiveEngine._write_verdict_snapshot(engine, "paper-only")  # type: ignore[arg-type]

    assert (tmp_path / "verdict_snapshot.json").exists()
    assert not (tmp_path / "verdict_snapshot.json.tmp").exists()


def test_verdict_snapshot_overwrites_prior_atomically(tmp_path: Path) -> None:
    """A subsequent write replaces the prior content fully — partial
    keys from a previous schema do not leak into the new file."""
    engine = _MinimalEngine(tmp_path)
    LiveEngine._write_verdict_snapshot(engine, "paper-only")  # type: ignore[arg-type]
    LiveEngine._write_verdict_snapshot(engine, "unsafe")  # type: ignore[arg-type]

    data = json.loads((tmp_path / "verdict_snapshot.json").read_text(encoding="utf-8"))
    assert data["verdict"] == "unsafe"
    assert set(data.keys()) == {"verdict", "observed_at_ms_utc"}
