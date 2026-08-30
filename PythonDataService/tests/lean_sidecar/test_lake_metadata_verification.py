"""The launcher's pre-mount metadata-bundle verification (#1879, PR C of
#1861).

Split out of ``test_lake_mount.py``, which pins a fixed, enumerated set of
four claims about the mount itself (see its own module docstring) -- these
are a fifth, independent claim: the launcher refuses to mount a lake root it
cannot prove is exactly the one the caller asked for, checked once against
the on-disk root-identity marker and once against the on-disk metadata
receipt, both filesystem-only (no Postgres; the launcher is a standalone
host process — see ``app.lean_sidecar.lake_mount``'s module docstring).
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.data_lake.path_policy import lake_subpath
from app.lean_sidecar.lake_mount import (
    LAKE_VOLUME_HOST_PATH_ENV,
    LakeMountError,
    launcher_host_base_root,
    launcher_host_lake_root,
    verify_lake_metadata_bundle,
)
from app.lean_sidecar.launcher.models import LaunchRequest
from app.lean_sidecar.launcher.service import LaunchRejectedError, launch
from app.lean_sidecar.workspace import resolve_workspace
from tests._helpers.lake_fixture import seed_lake_metadata, seed_lean_metadata_receipt

DUMMY_DIGEST = "sha256:0000000000000000000000000000000000000000000000000000000000000003"


@pytest.fixture
def _allow_dummy_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.lean_sidecar import config as sidecar_config
    from app.lean_sidecar import runner

    monkeypatch.setattr(sidecar_config, "ALLOWED_IMAGE_DIGESTS", frozenset({DUMMY_DIGEST}))
    monkeypatch.setattr(runner, "ALLOWED_IMAGE_DIGESTS", frozenset({DUMMY_DIGEST}))


class TestLauncherHostBaseRoot:
    """launcher_host_lake_root's base, extracted so the launcher's
    root-marker verification can resolve it independently of any one mode."""

    def test_returns_none_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(LAKE_VOLUME_HOST_PATH_ENV, raising=False)
        assert launcher_host_base_root() is None

    def test_returns_the_resolved_absolute_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        volume = tmp_path / "learn_ai_lean_data"
        volume.mkdir()
        monkeypatch.setenv(LAKE_VOLUME_HOST_PATH_ENV, str(volume))

        assert launcher_host_base_root() == volume.resolve()

    def test_relative_path_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LAKE_VOLUME_HOST_PATH_ENV, "var/lib/learn_ai_lean_data")

        with pytest.raises(LakeMountError, match="lake_volume_host_path_not_absolute"):
            launcher_host_base_root()

    def test_launcher_host_lake_root_joins_the_mode_onto_the_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        volume = tmp_path / "volume"
        volume.mkdir()
        monkeypatch.setenv(LAKE_VOLUME_HOST_PATH_ENV, str(volume))

        assert launcher_host_lake_root("raw") == launcher_host_base_root() / lake_subpath("raw")


class TestVerifyLakeMetadataBundle:
    """Two independent proofs — the root-identity marker at ``base_root``
    and the metadata receipt at ``lake_root`` — both filesystem-only, no
    Postgres."""

    def _seed_valid_bundle(
        self, base_root: Path, *, root_id: UUID, mode: str = "raw", digest: str = "sha256:test"
    ) -> Path:
        from app.data_lake import root_identity

        root_identity.init_empty_root(base_root, root_id)
        lake_root = base_root / lake_subpath(mode)
        seed_lean_metadata_receipt(lake_root, data_root_id=root_id, price_adjustment_mode=mode, lean_image_digest=digest)
        return lake_root

    def test_accepts_a_fully_valid_bundle(self, tmp_path: Path) -> None:
        root_id = uuid4()
        lake_root = self._seed_valid_bundle(tmp_path, root_id=root_id)

        verify_lake_metadata_bundle(
            lake_root=lake_root,
            base_root=tmp_path,
            expected_data_root_id=root_id,
            expected_price_adjustment_mode="raw",
            expected_lean_image_digest="sha256:test",
        )  # must not raise

    def test_refuses_when_the_root_marker_is_absent(self, tmp_path: Path) -> None:
        root_id = uuid4()
        lake_root = tmp_path / lake_subpath("raw")
        seed_lean_metadata_receipt(
            lake_root, data_root_id=root_id, price_adjustment_mode="raw", lean_image_digest="sha256:test"
        )

        with pytest.raises(LakeMountError, match="lake_root_identity_invalid"):
            verify_lake_metadata_bundle(
                lake_root=lake_root,
                base_root=tmp_path,
                expected_data_root_id=root_id,
                expected_price_adjustment_mode="raw",
                expected_lean_image_digest="sha256:test",
            )

    def test_refuses_a_marker_naming_a_different_root(self, tmp_path: Path) -> None:
        marker_root_id = uuid4()
        expected_root_id = uuid4()
        lake_root = self._seed_valid_bundle(tmp_path, root_id=marker_root_id)

        with pytest.raises(LakeMountError, match="lake_root_identity_invalid"):
            verify_lake_metadata_bundle(
                lake_root=lake_root,
                base_root=tmp_path,
                expected_data_root_id=expected_root_id,
                expected_price_adjustment_mode="raw",
                expected_lean_image_digest="sha256:test",
            )

    def test_refuses_when_the_receipt_is_absent(self, tmp_path: Path) -> None:
        from app.data_lake import root_identity

        root_id = uuid4()
        root_identity.init_empty_root(tmp_path, root_id)
        lake_root = tmp_path / lake_subpath("raw")
        lake_root.mkdir(parents=True)

        with pytest.raises(LakeMountError, match="lake_metadata_receipt_invalid"):
            verify_lake_metadata_bundle(
                lake_root=lake_root,
                base_root=tmp_path,
                expected_data_root_id=root_id,
                expected_price_adjustment_mode="raw",
                expected_lean_image_digest="sha256:test",
            )

    def test_refuses_a_receipt_from_a_different_mode(self, tmp_path: Path) -> None:
        root_id = uuid4()
        lake_root = self._seed_valid_bundle(tmp_path, root_id=root_id, mode="raw")

        with pytest.raises(LakeMountError, match="lake_metadata_receipt_invalid"):
            verify_lake_metadata_bundle(
                lake_root=lake_root,
                base_root=tmp_path,
                expected_data_root_id=root_id,
                expected_price_adjustment_mode="polygon_split_adjusted",
                expected_lean_image_digest="sha256:test",
            )

    def test_refuses_a_receipt_pinned_to_a_different_digest(self, tmp_path: Path) -> None:
        root_id = uuid4()
        lake_root = self._seed_valid_bundle(tmp_path, root_id=root_id, digest="sha256:old")

        with pytest.raises(LakeMountError, match="lake_metadata_receipt_invalid"):
            verify_lake_metadata_bundle(
                lake_root=lake_root,
                base_root=tmp_path,
                expected_data_root_id=root_id,
                expected_price_adjustment_mode="raw",
                expected_lean_image_digest="sha256:new",
            )

    def test_refuses_a_tampered_file(self, tmp_path: Path) -> None:
        """Acceptance criterion: tampering with a metadata file is detected before launch."""
        root_id = uuid4()
        lake_root = self._seed_valid_bundle(tmp_path, root_id=root_id)
        (lake_root / "symbol-properties" / "symbol-properties-database.csv").write_bytes(b"tampered\n")

        with pytest.raises(LakeMountError, match="lake_metadata_receipt_invalid"):
            verify_lake_metadata_bundle(
                lake_root=lake_root,
                base_root=tmp_path,
                expected_data_root_id=root_id,
                expected_price_adjustment_mode="raw",
                expected_lean_image_digest="sha256:test",
            )


class TestLaunchRefusesAnUnverifiedLakeMount:
    """``launch()`` itself refuses a lake-mount run whose bundle it cannot
    verify, before ever building the podman argv."""

    def _staged_workspace(self, tmp_artifacts_root: Path, run_id: str) -> None:
        resolve_workspace(run_id, tmp_artifacts_root).ensure_layout()

    def _request(self, *, data_root_id: UUID | None, run_id: str = "run_lake_verify") -> LaunchRequest:
        return LaunchRequest(
            run_id=run_id,
            image_digest=DUMMY_DIGEST,
            cpus=2.0,
            memory_mb=1024,
            pids_limit=256,
            wall_clock_timeout_s=60,
            workspace_max_mb=256,
            log_tail_bytes=4096,
            mount_lake_read_only=True,
            price_adjustment_mode="raw",
            data_root_id=data_root_id,
        )

    def test_refuses_when_the_request_carries_no_data_root_id(
        self, tmp_path: Path, tmp_artifacts_root: Path, _allow_dummy_digest: None
    ) -> None:
        self._staged_workspace(tmp_artifacts_root, "run_lake_verify")
        lake_root = tmp_path / "volume" / lake_subpath("raw")
        seed_lake_metadata(lake_root)  # no receipt needed -- rejected before that check runs

        with pytest.raises(LaunchRejectedError) as excinfo:
            launch(
                self._request(data_root_id=None),
                artifacts_root=tmp_artifacts_root,
                lake_root=lake_root,
                lake_base_root=tmp_path / "volume",
            )

        assert excinfo.value.reason == "lake_metadata_verification_missing_root_id"

    def test_refuses_an_unverifiable_bundle_with_no_silent_fallback(
        self, tmp_path: Path, tmp_artifacts_root: Path, _allow_dummy_digest: None
    ) -> None:
        self._staged_workspace(tmp_artifacts_root, "run_lake_verify")
        base_root = tmp_path / "volume"
        lake_root = base_root / lake_subpath("raw")
        seed_lake_metadata(lake_root)  # files exist, but no root marker and no receipt

        with pytest.raises(LaunchRejectedError) as excinfo:
            launch(
                self._request(data_root_id=uuid4()),
                artifacts_root=tmp_artifacts_root,
                lake_root=lake_root,
                lake_base_root=base_root,
            )

        assert excinfo.value.reason == "lake_metadata_verification_failed"

    def test_a_receipt_from_another_root_is_rejected(
        self, tmp_path: Path, tmp_artifacts_root: Path, _allow_dummy_digest: None
    ) -> None:
        """Acceptance criterion: a receipt from another root is rejected,
        and there is no silent root switching -- the launch is refused."""
        from app.data_lake import root_identity

        self._staged_workspace(tmp_artifacts_root, "run_lake_verify")
        base_root = tmp_path / "volume"
        marker_root_id = uuid4()
        root_identity.init_empty_root(base_root, marker_root_id)
        lake_root = base_root / lake_subpath("raw")
        seed_lean_metadata_receipt(
            lake_root, data_root_id=marker_root_id, price_adjustment_mode="raw", lean_image_digest=DUMMY_DIGEST
        )

        with pytest.raises(LaunchRejectedError) as excinfo:
            launch(
                self._request(data_root_id=uuid4()),  # a different root than the marker
                artifacts_root=tmp_artifacts_root,
                lake_root=lake_root,
                lake_base_root=base_root,
            )

        assert excinfo.value.reason == "lake_metadata_verification_failed"
