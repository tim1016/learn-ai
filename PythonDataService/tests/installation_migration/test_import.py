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
import os
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
    IMPORT_INCOMPLETE,
    IMPORT_RECEIPT,
    ImportRequest,
    run_import,
)
from app.installation_migration.tree import (
    sha256_file,
    tree_digest_from_dir,
    tree_digest_from_tar,
)
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
    steps: list[dict] | None = None,
    accept_dirty_source: bool = False,
    disk_free: Callable[[Path], int] | None = None,
) -> list[dict]:
    steps = [] if steps is None else steps
    run_import(
        ImportRequest(
            repo_root=repo_root,
            bundle_path=bundle,
            aside_dir=aside_dir,
            accept_dirty_source=accept_dirty_source,
        ),
        podman=podman,
        git=git or FakeGit(),
        emit=steps.append,
        **({} if disk_free is None else {"disk_free": disk_free}),
    )
    return steps


def _replace_folder_member(bundle: Path, target: Path, member: str, folder_tar: Path) -> None:
    """Swap one folder tar for another, re-describing it truthfully in the manifest.

    The member's SHA-256, size and content digest all match, so only the
    unsafe content itself can stop it.
    """
    payload = folder_tar.read_bytes()

    def describe(manifest: dict) -> None:
        for entry in manifest["folders"]:
            if entry["member"] == member:
                entry["size_bytes"] = len(payload)
                entry["sha256"] = sha256_file(folder_tar)
                entry["content_digest"] = tree_digest_from_tar(folder_tar)

    _rewrite_bundle(
        bundle,
        target,
        edit_manifest=describe,
        edit_member=lambda name, data: payload if name == member else data,
    )


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

    assert refused.value.reason == "env_files_missing"
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


def test_a_faithful_round_trip_resolves_a_lane_mounted_where_its_registry_says(
    tmp_path: Path, exported
) -> None:
    """The Live scratch clerk is recorded the way the dev topology mounts it
    (container path, the overlay's namespace), so it resolves on the new host
    and a re-approval report is a finding, not the fake's default. The Paper
    clerk cannot share that (namespace, root) — the registry's uniqueness
    index — so it alone is reported, never silently re-approved."""
    source, bundle = exported
    repo_root, podman = build_empty_destination(tmp_path)

    steps = _import(repo_root, podman, bundle)

    resolution = next(step for step in steps if step["step"] == "host-resolution")
    assert resolution["reapproval_required"] == [source.paper_clerk_id]
    live = next(entry for entry in resolution["clerks"] if entry["clerk_id"] == source.live_clerk_id)
    assert live["resolves"] is True
    assert steps[-1]["reapproval_required"] == [source.paper_clerk_id]


def test_volume_exports_have_no_dot_root_entry_like_real_podman(tmp_path: Path) -> None:
    podman = FakePodman(tmp_path / "podman")
    root = podman.add_volume("v")
    (root / "sub").mkdir()
    (root / "sub" / "a.txt").write_text("a", encoding="utf-8")

    podman.export_volume("v", tmp_path / "v.tar")

    with tarfile.open(tmp_path / "v.tar") as archive:
        assert sorted(archive.getnames()) == ["sub", "sub/a.txt"]


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


def _preexisting_destination(tmp_path: Path) -> tuple[Path, FakePodman]:
    repo_root, podman = build_empty_destination(tmp_path)
    old_pg = podman.add_volume(PG_VOLUME, labels={"old": "yes"})
    (old_pg / "old-cluster").write_text("previous database", encoding="utf-8")
    old_lake = repo_root / "data-lake-volume" / "lake"
    old_lake.mkdir(parents=True)
    (old_lake / "old.parquet").write_bytes(b"previous lake")
    return repo_root, podman


def _unchanged(repo_root: Path, podman: FakePodman) -> None:
    assert sorted(podman.volumes) == [PG_VOLUME]
    assert podman.removed == []
    assert (podman.volume_dir(PG_VOLUME) / "old-cluster").read_text(encoding="utf-8") == (
        "previous database"
    )
    assert (repo_root / "data-lake-volume" / "lake" / "old.parquet").read_bytes() == b"previous lake"
    assert not [p for p in repo_root.rglob("*") if ".migrate-import-" in p.name]


@pytest.mark.parametrize(
    "hostile",
    [
        lambda tar: tar.addfile(_symlink("hosts", "/etc/hosts")),
        lambda tar: tar.addfile(_symlink("up", "../../outside")),
        lambda tar: _add_file(tar, "../escaped.txt", b"x"),
    ],
    ids=["absolute_symlink", "escaping_symlink", "dot_dot_file"],
)
def test_an_unsafe_folder_member_refuses_before_anything_is_moved_aside(
    tmp_path: Path, exported, hostile: Callable[[tarfile.TarFile], None]
) -> None:
    _source, bundle = exported
    repo_root, podman = _preexisting_destination(tmp_path)
    folder_tar = tmp_path / "hostile-folder.tar"
    with tarfile.open(folder_tar, "x") as tar:
        _add_file(tar, "ok.txt", b"fine")
        hostile(tar)
    unsafe = tmp_path / "unsafe.tar"
    _replace_folder_member(bundle, unsafe, "folders/PythonDataService__lean-cache.tar", folder_tar)
    aside_dir = tmp_path / "aside"
    steps: list[dict] = []

    with pytest.raises(MigrationRefused) as refused:
        _import(repo_root, podman, unsafe, aside_dir=aside_dir, steps=steps)

    assert refused.value.reason == "bundle_member_unsafe"
    assert "moved-aside" not in [step["step"] for step in steps]
    assert not [path for path in aside_dir.rglob("*") if path.name.startswith("import-")]
    _unchanged(repo_root, podman)


def test_a_failure_during_restore_is_reported_recoverably(tmp_path: Path, exported) -> None:
    _source, bundle = exported
    repo_root, podman = _preexisting_destination(tmp_path)
    podman.fail_import.add(PAPER_VOLUME)
    aside_dir = tmp_path / "aside"

    with pytest.raises(MigrationRefused) as refused:
        _import(repo_root, podman, bundle, aside_dir=aside_dir)

    assert refused.value.reason == "restore_incomplete"
    details = refused.value.details
    [aside] = [path for path in aside_dir.iterdir() if path.name.startswith("import-")]
    assert details["aside"] == str(aside)
    assert details["aside_record"] == str(aside / ASIDE_RECORD)
    order = [volume.name for volume in BUNDLED_VOLUMES]
    assert details["restored_volumes"] == order[: order.index(PAPER_VOLUME)]
    assert details["restored_folders"] == []
    assert details["cause"]["reason"] == "podman_command_failed"
    assert str(aside) in refused.value.message
    assert "Do not start the stack" in refused.value.message
    receipt = json.loads((aside / IMPORT_INCOMPLETE).read_text(encoding="utf-8"))
    assert receipt["restored_volumes"] == details["restored_volumes"]
    assert receipt["stack"] == "stopped"
    record = json.loads((aside / ASIDE_RECORD).read_text(encoding="utf-8"))
    assert [volume["name"] for volume in record["volumes"]] == [PG_VOLUME]


def test_an_unreadable_aside_copy_is_never_followed_by_removing_the_volume(
    tmp_path: Path, exported
) -> None:
    _source, bundle = exported
    repo_root, podman = _preexisting_destination(tmp_path)
    (podman.volume_dir(PG_VOLUME) / "bulk.bin").write_bytes(os.urandom(8192))
    podman.truncate_export.add(PG_VOLUME)

    with pytest.raises(MigrationRefused) as refused:
        _import(repo_root, podman, bundle, aside_dir=tmp_path / "aside")

    assert refused.value.reason == "restore_incomplete"
    assert refused.value.details["cause"]["reason"] == "bundle_member_unreadable"
    assert podman.removed == []
    assert PG_VOLUME in podman.volumes


def test_insufficient_disk_refuses_before_anything_is_staged_or_moved(
    tmp_path: Path, exported
) -> None:
    _source, bundle = exported
    repo_root, podman = _preexisting_destination(tmp_path)
    aside_dir = tmp_path / "aside"
    steps: list[dict] = []

    with pytest.raises(MigrationRefused) as refused:
        _import(
            repo_root, podman, bundle, aside_dir=aside_dir, steps=steps, disk_free=lambda _p: 1024
        )

    assert refused.value.reason == "insufficient_disk_space"
    [shortfall, *_rest] = refused.value.details["filesystems"]
    assert shortfall["free_bytes"] == 1024
    assert shortfall["required_bytes"] > 3 * bundle.stat().st_size // 2
    assert [step["step"] for step in steps] == ["manifest", "code"]
    _unchanged(repo_root, podman)


@pytest.mark.parametrize(
    "missing", [Path(".env"), Path("PythonDataService") / ".env"], ids=["root", "data_plane"]
)
def test_import_refuses_without_the_host_env_files(
    tmp_path: Path, exported, missing: Path
) -> None:
    _source, bundle = exported
    repo_root, podman = build_empty_destination(tmp_path)
    (repo_root / missing).unlink()

    with pytest.raises(MigrationRefused) as refused:
        _import(repo_root, podman, bundle)

    assert refused.value.reason == "env_files_missing"
    assert refused.value.details["paths"] == [str(repo_root / missing)]
    assert "secret" not in refused.value.message.lower().replace("never carries a secret", "")
    assert podman.volumes == {}


def test_a_bundle_from_a_dirty_tree_needs_an_explicit_acknowledgement(tmp_path: Path) -> None:
    installation = build_installation(tmp_path)
    bundle = tmp_path / "bundle.tar"
    run_export(
        ExportRequest(
            repo_root=installation.repo_root,
            bundle_path=bundle,
            operator="inkant",
            change_ref="migrate-2026-09-22",
            allow_dirty_tree=True,
        ),
        lanes=installation.lanes,
        podman=installation.podman,
        git=FakeGit(dirty=True),
        emit=lambda _step: None,
    )
    repo_root, podman = build_empty_destination(tmp_path)

    with pytest.raises(MigrationRefused) as refused:
        _import(repo_root, podman, bundle)

    assert refused.value.reason == "source_tree_dirty_unacknowledged"
    assert podman.volumes == {}

    steps = _import(repo_root, podman, bundle, accept_dirty_source=True)
    assert steps[0]["source_tree_dirty"] is True
    assert steps[-1]["step"] == "complete"


def test_import_refuses_a_cluster_of_another_postgres_major(tmp_path: Path) -> None:
    installation = build_installation(tmp_path)
    (installation.podman.volume_dir(PG_VOLUME) / "PG_VERSION").write_text("15\n", encoding="utf-8")
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
    repo_root, podman = build_empty_destination(tmp_path)

    with pytest.raises(MigrationRefused) as refused:
        _import(repo_root, podman, bundle)

    assert refused.value.reason == "postgres_major_mismatch"
    assert refused.value.details == {"bundle_pg_version": "15", "destination_image_major": "16"}
    assert podman.volumes == {}


def _symlink(name: str, target: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    return info


def _add_file(tar: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    tar.addfile(info, io.BytesIO(payload))
