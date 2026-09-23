"""The go-live hold marker: read failing closed, released durably (#2269)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import go_live_hold
from app.services.go_live_hold import (
    GO_LIVE_HOLD_MARKER,
    GO_LIVE_RECEIPTS_DIRECTORY,
    GoLiveHoldMarker,
    GoLiveHoldUnreadableError,
    GoLiveReleaseFailedError,
    go_live_marker_bytes,
    read_go_live_hold,
    release_go_live_hold,
)

_MARKER = GoLiveHoldMarker(
    kind="learn-ai-go-live-hold",
    schema_version=1,
    written_at_ms=1_789_100_000_000,
    volume="learn-ai-alpaca-paper-clerk-data",
    source_commit="b" * 40,
    registry_id="reg_1",
)
_BAR_CHECK = {"symbol": "SPY", "bar_count": 390, "last_bar_end_ms": 1_789_000_000_000}


def _receipts(root: Path) -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((root / GO_LIVE_RECEIPTS_DIRECTORY).glob("*.json"))
    ]


def test_no_marker_is_no_hold(tmp_path: Path) -> None:
    state = read_go_live_hold(tmp_path)

    assert state.held is False
    assert state.problem is None


def test_a_marker_holds_and_says_which_bundle_put_it_there(tmp_path: Path) -> None:
    (tmp_path / GO_LIVE_HOLD_MARKER).write_bytes(go_live_marker_bytes(_MARKER))

    state = read_go_live_hold(tmp_path)

    assert state.held is True
    assert state.marker == _MARKER
    assert state.problem is None


def test_an_unparseable_marker_holds_with_the_problem_named(tmp_path: Path) -> None:
    (tmp_path / GO_LIVE_HOLD_MARKER).write_text("[]", encoding="utf-8")

    state = read_go_live_hold(tmp_path)

    assert state.held is True
    assert state.marker is None
    assert GO_LIVE_HOLD_MARKER in (state.problem or "")


def test_a_missing_lane_root_holds(tmp_path: Path) -> None:
    state = read_go_live_hold(tmp_path / "absent")

    assert state.held is True
    assert "not a readable directory" in (state.problem or "")


def test_a_marker_the_lane_cannot_read_holds(tmp_path: Path) -> None:
    # A directory where the file belongs: the read fails with an OSError
    # other than "not found", so the lane cannot tell — and holds.
    (tmp_path / GO_LIVE_HOLD_MARKER).mkdir()

    state = read_go_live_hold(tmp_path)

    assert state.held is True
    assert "could not be read" in (state.problem or "")


def test_release_removes_the_marker_and_records_what_it_released(tmp_path: Path) -> None:
    (tmp_path / GO_LIVE_HOLD_MARKER).write_bytes(go_live_marker_bytes(_MARKER))

    receipt = release_go_live_hold(
        tmp_path, operator="inkant", change_ref="go-live-1", bar_check=_BAR_CHECK, clock=lambda: 7
    )

    assert not (tmp_path / GO_LIVE_HOLD_MARKER).exists()
    assert read_go_live_hold(tmp_path).held is False
    assert receipt.was_held is True
    assert _receipts(tmp_path) == [receipt.to_json()]
    durable = _receipts(tmp_path)[0]
    assert durable["marker"] == _MARKER.model_dump()
    assert durable["bar_check"] == _BAR_CHECK
    assert durable["released_at_ms"] == 7


def test_release_of_an_unheld_lane_is_idempotent_and_still_recorded(tmp_path: Path) -> None:
    receipt = release_go_live_hold(
        tmp_path, operator="inkant", change_ref="go-live-1", bar_check=_BAR_CHECK
    )

    assert receipt.was_held is False
    assert len(_receipts(tmp_path)) == 1


def test_release_removes_a_corrupt_marker_and_names_the_problem(tmp_path: Path) -> None:
    (tmp_path / GO_LIVE_HOLD_MARKER).write_text("{", encoding="utf-8")

    receipt = release_go_live_hold(
        tmp_path, operator="inkant", change_ref="go-live-1", bar_check=_BAR_CHECK
    )

    assert not (tmp_path / GO_LIVE_HOLD_MARKER).exists()
    assert receipt.was_held is True
    assert receipt.marker is None
    assert receipt.marker_problem is not None


def test_release_refuses_when_the_lane_cannot_tell_whether_it_is_held(tmp_path: Path) -> None:
    (tmp_path / GO_LIVE_HOLD_MARKER).mkdir()

    with pytest.raises(GoLiveHoldUnreadableError):
        release_go_live_hold(
            tmp_path, operator="inkant", change_ref="go-live-1", bar_check=_BAR_CHECK
        )

    assert read_go_live_hold(tmp_path).held is True
    assert not (tmp_path / GO_LIVE_RECEIPTS_DIRECTORY).exists()


def test_a_receipt_that_cannot_be_written_leaves_the_lane_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Receipt first: no lane is released without its durable record."""
    (tmp_path / GO_LIVE_HOLD_MARKER).write_bytes(go_live_marker_bytes(_MARKER))

    def failing_write(_path: Path, _payload: bytes) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(go_live_hold, "atomic_write_bytes", failing_write)

    with pytest.raises(GoLiveReleaseFailedError) as failed:
        release_go_live_hold(
            tmp_path, operator="inkant", change_ref="go-live-1", bar_check=_BAR_CHECK
        )

    assert "No space left on device" in str(failed.value)
    assert "still starts no bots" in str(failed.value)
    assert read_go_live_hold(tmp_path).marker == _MARKER


def test_a_marker_that_cannot_be_removed_is_a_named_failure_after_its_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / GO_LIVE_HOLD_MARKER).write_bytes(go_live_marker_bytes(_MARKER))
    real_unlink = Path.unlink

    def refusing_unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == GO_LIVE_HOLD_MARKER:
            raise PermissionError(13, "Permission denied")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refusing_unlink)

    with pytest.raises(GoLiveReleaseFailedError) as failed:
        release_go_live_hold(
            tmp_path, operator="inkant", change_ref="go-live-1", bar_check=_BAR_CHECK
        )

    assert "could not be removed" in str(failed.value)
    assert read_go_live_hold(tmp_path).held is True
    [receipt] = _receipts(tmp_path)
    assert receipt["was_held"] is True
