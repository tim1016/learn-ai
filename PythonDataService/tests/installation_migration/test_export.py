"""``migrate-installation export`` against a scratch installation (#2268).

Pure fakes throughout: podman volumes are tmp directories, lanes answer from
a script. Pinned here: ``--check`` refuses a non-flat account by name and
touches nothing; a full export stops every bot, re-reads quiet, stops the
writers, and writes exactly the listed contents — no secret — without ever
draining a lane.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from app.installation_migration.bundle import MANIFEST_MEMBER, read_manifest
from app.installation_migration.contents import BUNDLED_FOLDERS, BUNDLED_VOLUMES
from app.installation_migration.errors import MigrationRefused
from app.installation_migration.export import ExportRequest, run_export
from tests.installation_migration._support import (
    LIVE_ACCOUNT,
    LIVE_VOLUME,
    PAPER_ACCOUNT,
    SOURCE_COMMIT,
    FakeGit,
    Installation,
    build_installation,
)


def _export(
    installation: Installation, bundle: Path, *, check_only: bool = False
) -> list[dict]:
    steps: list[dict] = []
    run_export(
        ExportRequest(
            repo_root=installation.repo_root,
            bundle_path=bundle,
            operator="inkant",
            change_ref="migrate-2026-09-22",
            check_only=check_only,
        ),
        lanes=installation.lanes,
        podman=installation.podman,
        git=FakeGit(),
        emit=steps.append,
    )
    return steps


def _open_position(installation: Installation) -> None:
    live = installation.lanes.quiet[installation.live_clerk_id]
    installation.lanes.quiet[installation.live_clerk_id] = {**live, "account_flat": False}


def _working_order(installation: Installation) -> None:
    paper = installation.lanes.quiet[installation.paper_clerk_id]
    installation.lanes.quiet[installation.paper_clerk_id] = {
        **paper,
        "broker_work_ended": False,
    }


def test_check_on_a_flat_fleet_passes_and_touches_nothing(tmp_path: Path) -> None:
    installation = build_installation(tmp_path)
    bundle = tmp_path / "bundle.tar"

    steps = _export(installation, bundle, check_only=True)

    assert steps[-1]["step"] == "check"
    assert steps[-1]["flat"] is True
    # A running bot is reported — export would stop it — never refused by --check.
    assert steps[-1]["bots_running_on"] == [installation.live_clerk_id]
    assert installation.lanes.stopped == []
    assert installation.podman.stopped == []
    assert not bundle.exists()


@pytest.mark.parametrize("make_open", [_open_position, _working_order])
def test_check_refuses_a_non_flat_account_naming_it_and_its_open_work(
    tmp_path: Path, make_open
) -> None:
    installation = build_installation(tmp_path)
    make_open(installation)
    bundle = tmp_path / "bundle.tar"

    with pytest.raises(MigrationRefused) as refused:
        _export(installation, bundle, check_only=True)

    assert refused.value.reason == "accounts_not_flat"
    [entry] = refused.value.details["accounts"]
    assert entry["account_id"] in {LIVE_ACCOUNT, PAPER_ACCOUNT}
    assert entry["account_id"] in refused.value.message
    assert entry["open"] in (
        ["the account is not flat"],
        ["a working order on the account has not ended"],
    )
    assert installation.lanes.stopped == []
    assert installation.podman.stopped == []
    assert not bundle.exists()


def test_export_refuses_a_non_flat_account_after_stopping_bots_and_writes_nothing(
    tmp_path: Path,
) -> None:
    installation = build_installation(tmp_path)
    _open_position(installation)
    bundle = tmp_path / "bundle.tar"

    with pytest.raises(MigrationRefused) as refused:
        _export(installation, bundle)

    assert refused.value.reason == "accounts_not_flat"
    assert "stays stopped" in refused.value.message
    assert sorted(installation.lanes.stopped) == sorted(
        [installation.live_clerk_id, installation.paper_clerk_id]
    )
    # Containers keep running and nothing is copied: the refusal comes first.
    assert installation.podman.stopped == []
    assert not bundle.exists()
    assert list(tmp_path.glob("bundle.tar*")) == []


def test_export_writes_exactly_the_listed_contents_plus_the_manifest(tmp_path: Path) -> None:
    installation = build_installation(tmp_path)
    bundle = tmp_path / "out" / "bundle.tar"

    steps = _export(installation, bundle)

    assert steps[-1]["step"] == "complete"
    with tarfile.open(bundle) as archive:
        names = archive.getnames()
    assert names[0] == MANIFEST_MEMBER
    assert sorted(names[1:]) == sorted(
        [volume.member for volume in BUNDLED_VOLUMES]
        + [folder.member for folder in BUNDLED_FOLDERS]
    )
    manifest = read_manifest(bundle)
    assert manifest.source_commit == SOURCE_COMMIT
    assert isinstance(manifest.created_at_ms, int)
    assert {entry.name for entry in manifest.volumes} == {v.name for v in BUNDLED_VOLUMES}
    # No lane was drained and no assignment changed state.
    assert {clerk.lifecycle_state for clerk in manifest.registry.clerks} == {"provisioned"}
    assert {row.state for row in manifest.registry.assignments} == {"effective"}
    assert [lane.stop_receipt_id for lane in manifest.lanes] == [
        f"rcpt-{installation.live_clerk_id}",
        f"rcpt-{installation.paper_clerk_id}",
    ]
    # The staging area is gone; only the bundle remains.
    assert sorted(path.name for path in bundle.parent.iterdir()) == ["bundle.tar"]


def test_export_stops_bots_before_it_stops_containers_clerks_first_postgres_last(
    tmp_path: Path,
) -> None:
    installation = build_installation(tmp_path)

    _export(installation, tmp_path / "bundle.tar")

    stopped = installation.podman.stopped
    assert stopped[-1] == "my-postgres"
    assert stopped.index("alpaca-live-clerk") < stopped.index("polygon-data-service")
    assert stopped.index("alpaca-paper-clerk") < stopped.index("polygon-data-service")


def test_the_manifest_records_markers_and_authority_generations(tmp_path: Path) -> None:
    installation = build_installation(tmp_path)
    bundle = tmp_path / "bundle.tar"

    _export(installation, bundle)

    manifest = read_manifest(bundle)
    live = next(v for v in manifest.clerk_volumes if v.volume == LIVE_VOLUME)
    assert live.marker.clerk_id == installation.live_clerk_id
    assert [account.account_id for account in live.accounts] == [LIVE_ACCOUNT]
    assert live.accounts[0].authority_generation >= 1
    assignment = next(
        row for row in manifest.registry.assignments if row.clerk_id == installation.live_clerk_id
    )
    assert assignment.assignment_generation >= 1
    assert assignment.confirmed_binding_generation == 1


def test_the_bundle_carries_no_secret_file(tmp_path: Path) -> None:
    installation = build_installation(tmp_path)
    env = installation.repo_root / "deploy" / "fleet" / "env"
    env.mkdir(parents=True)
    (env / "live.env").write_text("ALPACA_CREDENTIAL_LIVE_SECRET_KEY=SENTINEL-SECRET-9f3c\n", encoding="utf-8")
    (installation.repo_root / ".env").write_text("POSTGRES_PASSWORD=SENTINEL-SECRET-9f3c\n", encoding="utf-8")
    (installation.repo_root / "compose.override.yaml").write_text("{}", encoding="utf-8")
    bundle = tmp_path / "bundle.tar"

    _export(installation, bundle)

    offending: list[str] = []
    with tarfile.open(bundle) as outer:
        for member in outer.getmembers():
            name = Path(member.name).name
            if name.endswith(".env") or name in {"compose.override.yaml", "compose.override.yml"}:
                offending.append(member.name)
            if member.name.endswith(".tar"):
                inner = outer.extractfile(member)
                assert inner is not None
                with tarfile.open(fileobj=inner) as nested:
                    offending.extend(
                        f"{member.name}:{n}"
                        for n in nested.getnames()
                        if Path(n).name.endswith(".env")
                        or Path(n).name in {"compose.override.yaml", "compose.override.yml"}
                    )
        raw = b"".join(
            outer.extractfile(m).read()  # type: ignore[union-attr]
            for m in outer.getmembers()
            if m.isreg()
        )
    assert offending == []
    assert b"SENTINEL-SECRET-9f3c" not in raw


def test_a_secret_inside_a_bundled_folder_refuses_before_any_bot_stops(tmp_path: Path) -> None:
    installation = build_installation(tmp_path)
    (installation.repo_root / "PythonDataService" / "artifacts" / "stray.env").write_text(
        "K=V", encoding="utf-8"
    )

    with pytest.raises(MigrationRefused) as refused:
        _export(installation, tmp_path / "bundle.tar")

    assert refused.value.reason == "secret_in_bundle_source"
    assert any(path.endswith("stray.env") for path in refused.value.details["paths"])
    assert installation.lanes.stopped == []


def test_a_missing_volume_refuses_before_any_bot_stops(tmp_path: Path) -> None:
    installation = build_installation(tmp_path)
    del installation.podman.volumes["learn-ai-alpaca-clerk-qualification-data"]

    with pytest.raises(MigrationRefused) as refused:
        _export(installation, tmp_path / "bundle.tar")

    assert refused.value.reason == "volume_missing"
    assert refused.value.details["volumes"] == ["learn-ai-alpaca-clerk-qualification-data"]
    assert installation.lanes.stopped == []


def test_a_draining_lane_refuses(tmp_path: Path) -> None:
    installation = build_installation(tmp_path)
    live = installation.lanes.lanes[0]
    installation.lanes.lanes[0] = type(live)(
        live.clerk_id, live.broker, "draining", live.display_label
    )

    with pytest.raises(MigrationRefused) as refused:
        _export(installation, tmp_path / "bundle.tar", check_only=True)

    assert refused.value.reason == "lane_draining"
    assert refused.value.details["clerk_ids"] == [installation.live_clerk_id]


def test_an_existing_bundle_is_never_overwritten(tmp_path: Path) -> None:
    installation = build_installation(tmp_path)
    bundle = tmp_path / "bundle.tar"
    bundle.write_bytes(b"precious")

    with pytest.raises(MigrationRefused) as refused:
        _export(installation, bundle)

    assert refused.value.reason == "bundle_exists"
    assert bundle.read_bytes() == b"precious"
