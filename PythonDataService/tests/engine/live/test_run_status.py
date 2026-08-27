"""Tests for the retained atomic JSON utility in ``app.engine.live.run_status``.

The ``ExitReason`` / ``RunStatusSidecar`` coverage that used to live here went
with those models in PR-C of #1813: their only production importers were
``routers/live_runs.py`` and ``services/live_run_state.py``, both retired by
this decommission, which left this file as their sole referrer repo-wide. A
test that is the last thing keeping a model alive is not coverage.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.engine.live.run_status import _atomic_write_json

# ---------------------------------------------------------------------------
# _atomic_write_json
# ---------------------------------------------------------------------------


def test_atomic_write_json_creates_file(tmp_path: Path):
    target = tmp_path / "output.json"
    payload = {"key": "value", "number": 42}
    _atomic_write_json(target, payload)
    assert target.exists()


def test_atomic_write_json_no_tmp_file_left(tmp_path: Path):
    target = tmp_path / "output.json"
    _atomic_write_json(target, {"x": 1})
    tmp = target.with_suffix(".tmp")
    assert not tmp.exists()


def test_atomic_write_json_content_correct(tmp_path: Path):
    target = tmp_path / "output.json"
    payload = {"schema_version": 1, "run_id": "abc123"}
    _atomic_write_json(target, payload)

    read_back = json.loads(target.read_text(encoding="utf-8"))
    assert read_back["schema_version"] == 1
    assert read_back["run_id"] == "abc123"
