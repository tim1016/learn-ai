"""Workspace path-under-root + run-id validation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.lean_sidecar.workspace import (
    WorkspaceError,
    resolve_workspace,
    validate_run_id,
)


class TestValidateRunId:
    @pytest.mark.parametrize(
        "good",
        [
            "abc",
            "run_001",
            "a1-b2-c3",
            "z" * 64,
            "0123",
        ],
    )
    def test_accepts_valid_slugs(self, good: str) -> None:
        validate_run_id(good)  # no exception

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "ab",  # too short
            "z" * 65,  # too long
            "ABC",  # uppercase
            "run id",  # space
            "../etc/passwd",
            "run/sub",
            ".hidden",
            "-leading-hyphen",
            "_leading_underscore",
            "run.id",
        ],
    )
    def test_rejects_bad_slugs(self, bad: str) -> None:
        with pytest.raises(WorkspaceError):
            validate_run_id(bad)


class TestResolveWorkspace:
    def test_resolves_under_root(self, tmp_artifacts_root: Path) -> None:
        ws = resolve_workspace("run_0001", tmp_artifacts_root)
        # The resolved root must be a child of the configured root.
        assert ws.root.parent == tmp_artifacts_root
        # And all the layout pieces are under the workspace root.
        assert ws.project_dir.parent == ws.workspace_dir
        assert ws.data_dir.parent == ws.workspace_dir
        assert ws.output_dir.parent == ws.workspace_dir
        assert ws.launcher_dir.parent == ws.workspace_dir
        assert ws.manifest_path.parent == ws.root

    def test_ensure_layout_idempotent(self, tmp_artifacts_root: Path) -> None:
        ws = resolve_workspace("run_0002", tmp_artifacts_root)
        ws.ensure_layout()
        ws.ensure_layout()  # second call must not raise

        for d in (
            ws.project_dir,
            ws.data_dir,
            ws.output_dir,
            ws.launcher_dir,
            ws.normalized_dir,
        ):
            assert d.is_dir()

    def test_rejects_bad_run_id(self, tmp_artifacts_root: Path) -> None:
        with pytest.raises(WorkspaceError):
            resolve_workspace("../escape", tmp_artifacts_root)
