"""``migrate-installation import`` and the full round trip (#2268).

A bundle is exported from one scratch installation and imported into
another; pure fakes throughout. Pinned: the refusals that come before
anything changes (env files, older code, a live stack, a tampered bundle, a
volume identity that disagrees with the manifest), that pre-existing data is
moved aside and never deleted, and that registry, clerk volumes and lake
arrive identical.
"""

from __future__ import annotations

import io
import json
import shutil
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

from app.installation_migration.bundle import MANIFEST_MEMBER
from app.installation_migration.contents import BUNDLED_FOLDERS, BUNDLED_VOLUMES
from app.installation_migration.errors import MigrationRefused
from app.installation_migration.export import ExportRequest, run_export
from app.installation_migration.facts import read_clerk_volume_facts, read_registry_facts
from app.installation_migration.importer import (
    ASIDE_RECORD,
    IMPORT_RECEIPT,
    ImportRequest,
    run_import,
)
from app.installation_migration.tree import tree_digest_from_dir
from tests.installation_migration._support import (
    CONTROL_VOLUME,
    LIVE_VOLUME,
    PAPER_VOLUME,
    PG_VOLUME,
    SOURCE_COMMIT,
    FakeGit,
    FakePodman,
    build_empty_destination,
    build_installation,
)


@pytest.fixture
def exported(tmp_path: Path):
    installation = build_installation(tmp_path)
    bundle = tmp_path / "bundle.tar"
    run_export(
        ExportRequest(
            repo_root=installation.repo_root,
            bundle_path=bundle,
            operator="inkant",
            change_ref="migrate-2026-09-22",
        ),
        lanes=installation.lanes,
        podman=installation.podman,
        git=FakeGit(),
        emit=lambda _step: None,
    )
    return installation, bundle


def _import(
    repo_root: Path,
    podman: FakePodman,
    bundle: Path,
    *,
    git: FakeGit | None = None,
    aside_dir: Path | None = None,
) -> list[dict]:
    steps: list[dict] = []
    run_import(
        ImportRequest(repo_root=repo_root, bundle_path=bundle, aside_dir=aside_dir),
        podman=podman,
        git=git or FakeGit(),
        emit=steps.append,
    )
    return steps


def _rewrite_bundle(
    bundle: Path,
    target: Path,
    *,
    edit_member: Callable[[str, bytes], bytes] | None = None,
    edit_manifest: Callable[[dict], object] | None = None,
    rename: dict[str, str] | None = None,
) -> None:
    """Copy a bundle, altering one member's bytes, name or the manifest in flight."""
    with tarfile.open(bundle) as source, tarfile.open(target, "x") as sink:
        for member in source.getmembers():
            data = source.extractfile(member).read()  # type: ignore[union-attr]
            if member.name == MANIFEST_MEMBER and edit_manifest is not None:
                manifest = json.loads(data)
                edit_manifest(manifest)
                data = json.dumps(manifest).encode("utf-8")
            elif edit_member is not None:
                data = edit_member(member.name, data)
            info = tarfile.TarInfo((rename or {}).get(member.name, member.name))
            info.size = len(data)
            sink.addfile(info, io.BytesIO(data))


def test_round_trip_restores_registry_clerk_volumes_and_lake_identically(
    tmp_path: Path, exported
) -> None:
    source, bundle = exported
    repo_root, podman = build_empty_destination(tmp_path)

    steps = _import(repo_root, podman, bundle)

    assert steps[-1]["step"] == "complete"
    assert steps[-1]["stack"] == "stopped"
    assert read_registry_facts(podman.volume_dir(CONTROL_VOLUME)) == read_registry_facts(
        source.podman.volume_dir(CONTROL_VOLUME)
    )
    for volume in (LIVE_VOLUME, PAPER_VOLUME):
        assert read_clerk_volume_facts(volume, podman.volume_dir(volume)) == read_clerk_volume_facts(
            volume, source.podman.volume_dir(volume)
        )
    for volume in BUNDLED_VOLUMES:
        assert tree_digest_from_dir(podman.volume_dir(volume.name)) == tree_digest_from_dir(
            source.podman.volume_dir(volume.name)
        )
        assert podman.volumes[volume.name].labels == source.podman.volumes[volume.name].labels
    for folder in BUNDLED_FOLDERS:
        assert tree_digest_from_dir(repo_root / folder.key) == tree_digest_from_dir(
            source.repo_root / folder.key
        )
    # The stop receipts travelled with the lane volumes they describe.
    assert list((podman.volume_dir(LIVE_VOLUME) / "lane_stop_all_receipts").glob("*.json"))
    # Import never starts anything.
    assert podman.containers == {}


def test_import_moves_existing_data_aside_and_deletes_nothing(tmp_path: Path, exported) -> None:
    _source, bundle = exported
    repo_root, podman = build_empty_destination(tmp_path)
    old_pg = podman.add_volume(PG_VOLUME, labels={"old": "yes"})
    (old_pg / "old-cluster").write_text("previous database", encoding="utf-8")
    old_lake = repo_root / "data-lake-volume" / "lake"
    old_lake.mkdir(parents=True)
    (old_lake / "old.parquet").write_bytes(b"previous lake")
    aside_dir = tmp_path / "aside"

    steps = _import(repo_root, podman, bundle, aside_dir=aside_dir)

    [aside] = list(aside_dir.iterdir())
    record = json.loads((aside / ASIDE_RECORD).read_text(encoding="utf-8"))
    [moved_volume] = record["volumes"]
    assert moved_volume["name"] == PG_VOLUME
    assert moved_volume["labels"] == {"old": "yes"}
    with tarfile.open(moved_volume["copy"]) as preserved:
        member = next(m for m in preserved.getmembers() if m.name.endswith("old-cluster"))
        assert preserved.extractfile(member).read() == b"previous database"  # type: ignore[union-attr]
    [moved_folder] = record["folders"]
    assert moved_folder["key"] == "data-lake-volume"
    assert (Path(moved_folder["copy"]) / "lake" / "old.parquet").read_bytes() == b"previous lake"
    assert not (repo_root / "data-lake-volume" / "lake" / "old.parquet").exists()
    assert (aside / IMPORT_RECEIPT).is_file()
    assert steps[-1]["step"] == "complete"


def test_import_refuses_without_the_fleet_env_files(tmp_path: Path, exported) -> None:
    _source, bundle = exported
    repo_root, podman = build_empty_destination(tmp_path)
    (repo_root / "deploy" / "fleet" / "env" / "live.env").unlink()

    with pytest.raises(MigrationRefused) as refused:
        _import(repo_root, podman, bundle)

    assert refused.value.reason == "fleet_env_files_missing"
    assert [Path(p).name for p in refused.value.details["paths"]] == ["live.env"]
    assert podman.volumes == {}


def test_import_refuses_older_destination_code(tmp_path: Path, exported) -> None:
    _source, bundle = exported
    repo_root, podman = build_empty_destination(tmp_path)
    older = FakeGit(head="b" * 40, known={SOURCE_COMMIT, "b" * 40})

    with pytest.raises(MigrationRefused) as refused:
        _import(repo_root, podman, bundle, git=older)

    assert refused.value.reason == "destination_code_older"
    assert podman.volumes == {}


def test_import_accepts_newer_destination_code(tmp_path: Path, exported) -> None:
    _source, bundle = exported
    repo_root, podman = build_empty_destination(tmp_path)
    newer = FakeGit(
        head="c" * 40, known={SOURCE_COMMIT, "c" * 40}, descendants={SOURCE_COMMIT: {"c" * 40}}
    )

    steps = _import(repo_root, podman, bundle, git=newer)

    assert steps[-1]["step"] == "complete"


def test_import_refuses_a_checkout_that_does_not_know_the_source_commit(
    tmp_path: Path, exported
) -> None:
    _source, bundle = exported
    repo_root, podman = build_empty_destination(tmp_path)

    with pytest.raises(MigrationRefused) as refused:
        _import(repo_root, podman, bundle, git=FakeGit(head="d" * 40, known={"d" * 40}))

    assert refused.value.reason == "source_commit_unknown"


def test_import_refuses_while_a_container_holds_a_bundled_volume(
    tmp_path: Path, exported
) -> None:
    _source, bundle = exported
    repo_root, podman = build_empty_destination(tmp_path)
    podman.add_volume(PG_VOLUME)
    podman.add_container("my-postgres", running=False, volumes=[PG_VOLUME])

    with pytest.raises(MigrationRefused) as refused:
        _import(repo_root, podman, bundle)

    assert refused.value.reason == "destination_stack_present"
    assert "my-postgres" in refused.value.message
    assert podman.removed == []


def test_import_fails_loudly_on_a_tampered_member(tmp_path: Path, exported) -> None:
    _source, bundle = exported
    repo_root, podman = build_empty_destination(tmp_path)
    tampered = tmp_path / "tampered.tar"

    def flip_one_byte(name: str, data: bytes) -> bytes:
        if name != f"volumes/{PG_VOLUME}.tar":
            return data
        index = len(data) // 3
        return data[:index] + bytes([data[index] ^ 0xFF]) + data[index + 1 :]

    _rewrite_bundle(bundle, tampered, edit_member=flip_one_byte)

    with pytest.raises(MigrationRefused) as refused:
        _import(repo_root, podman, tampered)

    assert refused.value.reason == "bundle_member_hash_mismatch"
    assert refused.value.details["member"] == f"volumes/{PG_VOLUME}.tar"
    assert podman.volumes == {}


def test_import_fails_loudly_on_a_volume_id_mismatch(tmp_path: Path, exported) -> None:
    _source, bundle = exported
    repo_root, podman = build_empty_destination(tmp_path)
    altered = tmp_path / "altered.tar"

    def claim_another_volume_id(manifest: dict) -> None:
        for entry in manifest["clerk_volumes"]:
            if entry["volume"] == LIVE_VOLUME:
                entry["marker"]["volume_id"] = "vol_someone_else"

    _rewrite_bundle(bundle, altered, edit_manifest=claim_another_volume_id)

    with pytest.raises(MigrationRefused) as refused:
        _import(repo_root, podman, altered)

    assert refused.value.reason == "volume_identity_mismatch"
    assert refused.value.details["volume"] == LIVE_VOLUME
    assert podman.volumes == {}


def test_import_fails_loudly_on_a_registry_generation_mismatch(
    tmp_path: Path, exported
) -> None:
    _source, bundle = exported
    repo_root, podman = build_empty_destination(tmp_path)
    altered = tmp_path / "altered.tar"

    def bump_generation(manifest: dict) -> None:
        manifest["registry"]["assignments"][0]["assignment_generation"] += 1

    _rewrite_bundle(bundle, altered, edit_manifest=bump_generation)

    with pytest.raises(MigrationRefused) as refused:
        _import(repo_root, podman, altered)

    assert refused.value.reason == "registry_identity_mismatch"
    assert refused.value.details["fields"] == ["assignments"]


def test_import_refuses_a_member_the_manifest_does_not_declare(
    tmp_path: Path, exported
) -> None:
    _source, bundle = exported
    repo_root, podman = build_empty_destination(tmp_path)
    smuggled = tmp_path / "smuggled.tar"
    shutil.copy2(bundle, smuggled)
    with tarfile.open(smuggled, "a") as archive:
        info = tarfile.TarInfo("deploy/fleet/env/live.env")
        payload = b"SECRET=1"
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    with pytest.raises(MigrationRefused) as refused:
        _import(repo_root, podman, smuggled)

    assert refused.value.reason == "bundle_member_unexpected"
    assert podman.volumes == {}


def test_import_reports_that_a_scratch_registry_needs_reapproval(
    tmp_path: Path, exported
) -> None:
    _source, bundle = exported
    repo_root, podman = build_empty_destination(tmp_path)

    steps = _import(repo_root, podman, bundle)

    resolution = next(step for step in steps if step["step"] == "host-resolution")
    # The scratch clerks were provisioned on tmp roots, which no service here
    # mounts: reported, never silently re-approved.
    assert len(resolution["reapproval_required"]) == 2
    assert steps[-1]["reapproval_required"] == resolution["reapproval_required"]


_LEAN_CACHE_MEMBER = "folders/PythonDataService__lean-cache.tar"


@pytest.mark.parametrize(
    "hostile_member",
    ["../../../ESCAPED.tar", "<absolute>", "folders/../../ESCAPED.tar"],
    ids=["dot_dot", "absolute", "nested_dot_dot"],
)
def test_import_refuses_a_manifest_member_outside_the_declared_layout(
    tmp_path: Path, exported, hostile_member: str
) -> None:
    _source, bundle = exported
    repo_root, podman = build_empty_destination(tmp_path)
    hostile = tmp_path / "hostile.tar"
    if hostile_member == "<absolute>":
        hostile_member = str(tmp_path / "outside" / "ESCAPED-absolute.tar")

    def point_member_elsewhere(manifest: dict) -> None:
        for entry in manifest["folders"]:
            if entry["member"] == _LEAN_CACHE_MEMBER:
                entry["member"] = hostile_member

    _rewrite_bundle(
        bundle,
        hostile,
        edit_manifest=point_member_elsewhere,
        rename={_LEAN_CACHE_MEMBER: hostile_member},
    )

    with pytest.raises(MigrationRefused) as refused:
        _import(repo_root, podman, hostile)

    assert refused.value.reason == "bundle_manifest_invalid"
    assert not list(tmp_path.rglob("ESCAPED*.tar"))
    assert podman.volumes == {}


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda manifest: manifest["volumes"][0].pop("sha256"),
        lambda manifest: manifest.pop("registry"),
        lambda manifest: manifest["folders"][0].update(size_bytes="big"),
        lambda manifest: manifest.update(created_at_ms="yesterday"),
        lambda manifest: manifest.update(source_tree_dirty=1),
        lambda manifest: manifest["volumes"][0].update(size_bytes=True),
        lambda manifest: manifest["registry"]["clerks"][0].update(volume_id=7),
        lambda manifest: manifest["volumes"].append(dict(manifest["volumes"][0])),
    ],
    ids=[
        "missing_sha256",
        "missing_registry",
        "size_not_int",
        "instant_not_int",
        "flag_not_bool",
        "bool_as_int",
        "nested_wrong_type",
        "duplicated_volume",
    ],
)
def test_a_malformed_manifest_refuses_with_a_reason_never_a_traceback(
    tmp_path: Path, exported, corrupt: Callable[[dict], object]
) -> None:
    _source, bundle = exported
    repo_root, podman = build_empty_destination(tmp_path)
    malformed = tmp_path / "malformed.tar"
    _rewrite_bundle(bundle, malformed, edit_manifest=corrupt)

    with pytest.raises(MigrationRefused) as refused:
        _import(repo_root, podman, malformed)

    assert refused.value.reason == "bundle_manifest_invalid"
    assert refused.value.details["errors"]
    assert podman.volumes == {}
