"""Phase 5e — unit tests for _count_bars_consumed.

The helper parses ``<workspace>/output/storage/observations.csv``
(written by the trusted sample's OnData) into the manifest's
``bars_consumed_by_symbol`` field — closes the other half of
invariant #16.

Pure-function tests: no LEAN container, just file shapes against the
helper's contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.lean_sidecar.workspace import Workspace
from app.services.lean_sidecar_service import _count_bars_consumed


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    ws = Workspace(run_id="ut_bars_consumed", artifacts_root=tmp_path, root=tmp_path / "ut")
    ws.ensure_layout()
    return ws


class TestCountBarsConsumed:
    def test_missing_file_returns_empty_dict(self, workspace: Workspace) -> None:
        """A run that didn't write observations.csv (e.g., a user
        algorithm that doesn't follow the trusted-sample convention)
        must NOT crash the manifest writer — return ``{}`` so the
        manifest records "no bar-consumption evidence" honestly."""
        assert _count_bars_consumed(workspace, "SPY") == {}

    def test_empty_file_returns_empty_dict(self, workspace: Workspace) -> None:
        obs_path = workspace.object_store_dir / "observations.csv"
        obs_path.write_text("", encoding="utf-8")
        assert _count_bars_consumed(workspace, "SPY") == {}

    def test_header_only_returns_empty_dict(self, workspace: Workspace) -> None:
        """A run that wrote the header (Initialize) but consumed no
        bars (OnData never fired — empty data window) reports 0 by
        returning ``{}``. The empty dict and `{"SPY": 0}` would both
        be valid; ``{}`` matches the "missing file" semantic so
        downstream consumers only branch one way."""
        obs_path = workspace.object_store_dir / "observations.csv"
        obs_path.write_text("ms_utc,close\n", encoding="utf-8")
        assert _count_bars_consumed(workspace, "SPY") == {}

    def test_single_bar_counts_one(self, workspace: Workspace) -> None:
        obs_path = workspace.object_store_dir / "observations.csv"
        obs_path.write_text("ms_utc,close\n1736121600000,580.50\n", encoding="utf-8")
        assert _count_bars_consumed(workspace, "SPY") == {"SPY": 1}

    def test_multiple_bars_count_excludes_header(self, workspace: Workspace) -> None:
        rows = "ms_utc,close\n" + "\n".join(
            f"{1736121600000 + i * 60000},{580.50 + i * 0.01}" for i in range(30)
        )
        obs_path = workspace.object_store_dir / "observations.csv"
        obs_path.write_text(rows + "\n", encoding="utf-8")
        assert _count_bars_consumed(workspace, "SPY") == {"SPY": 30}

    def test_trailing_blank_lines_not_counted(self, workspace: Workspace) -> None:
        """A trailing newline (or multiple blanks from a flaky writer)
        must not add phantom bars to the count."""
        obs_path = workspace.object_store_dir / "observations.csv"
        obs_path.write_text(
            "ms_utc,close\n1736121600000,580.50\n\n\n", encoding="utf-8"
        )
        assert _count_bars_consumed(workspace, "SPY") == {"SPY": 1}

    def test_symbol_is_upper_cased(self, workspace: Workspace) -> None:
        """The manifest key is the canonical uppercase symbol — same
        convention the rest of the staging layer uses. A request with
        ``symbol="spy"`` should not produce a separate
        ``"spy"``-keyed entry."""
        obs_path = workspace.object_store_dir / "observations.csv"
        obs_path.write_text("ms_utc,close\n1,100\n", encoding="utf-8")
        assert _count_bars_consumed(workspace, "spy") == {"SPY": 1}

    def test_unreadable_file_returns_empty_dict_with_warning(
        self, workspace: Workspace, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OSError on read (e.g., transient disk error) must return
        ``{}`` AND log a warning so the operator can find the bad
        workspace — per the no-silent-exception-handler rule."""
        import logging

        from app.services import lean_sidecar_service

        obs_path = workspace.object_store_dir / "observations.csv"
        obs_path.write_text("ms_utc,close\n1,100\n", encoding="utf-8")

        def _raise(*a, **k):
            raise OSError("simulated disk error")

        monkeypatch.setattr(Path, "read_text", _raise)
        with caplog.at_level(logging.WARNING, logger=lean_sidecar_service.__name__):
            result = _count_bars_consumed(workspace, "SPY")
        assert result == {}
        assert any("observations.csv" in rec.message for rec in caplog.records)


def test_helper_does_not_create_files(tmp_path: Path) -> None:
    """Pure-read contract: a call against a fresh workspace must not
    create observations.csv just to read it."""
    ws = Workspace(run_id="ut_no_create", artifacts_root=tmp_path, root=tmp_path / "ut")
    ws.ensure_layout()
    _count_bars_consumed(ws, "SPY")
    assert not (ws.object_store_dir / "observations.csv").exists()
