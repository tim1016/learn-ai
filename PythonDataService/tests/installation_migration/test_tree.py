"""One canonical walk and content digest for every bundled tree (#2268).

A folder is bundled as a tar built from the walk, verified on import from the
restored directory, and a podman volume is verified from its exported tar —
so the directory digest and the tar digest must agree by construction, and
anything the walk cannot carry faithfully refuses by name.
"""

from __future__ import annotations

import os
import tarfile
from pathlib import Path

import pytest

from app.installation_migration.errors import MigrationRefused
from app.installation_migration.tree import (
    build_folder_tar,
    extract_tar,
    require_bundleable,
    sha256_file,
    tree_digest_from_dir,
    tree_digest_from_tar,
)


def _populate(root: Path) -> None:
    (root / "lake" / "bars").mkdir(parents=True)
    (root / "lake" / "bars" / "spy.parquet").write_bytes(b"\x00\x01parquet")
    (root / "empty").mkdir()
    (root / ".gitkeep").write_text("", encoding="utf-8")
    (root / "notes.txt").write_text("hello", encoding="utf-8")
    os.symlink("notes.txt", root / "latest")


def test_folder_tar_digest_equals_the_directory_digest(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _populate(source)
    archive = tmp_path / "folder.tar"

    build_folder_tar(source, archive)

    assert tree_digest_from_tar(archive) == tree_digest_from_dir(source)


def test_extracting_the_tar_reproduces_the_same_digest(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _populate(source)
    archive = tmp_path / "folder.tar"
    build_folder_tar(source, archive)
    restored = tmp_path / "restored"
    restored.mkdir()

    extract_tar(archive, restored)

    assert tree_digest_from_dir(restored) == tree_digest_from_dir(source)
    assert (restored / "lake" / "bars" / "spy.parquet").read_bytes() == b"\x00\x01parquet"
    assert os.readlink(restored / "latest") == "notes.txt"


def test_digest_changes_when_one_byte_of_content_changes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _populate(root)
    before = tree_digest_from_dir(root)

    (root / "notes.txt").write_text("hellO", encoding="utf-8")

    assert tree_digest_from_dir(root) != before


def test_digest_sees_an_added_empty_directory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _populate(root)
    before = tree_digest_from_dir(root)

    (root / "another-empty").mkdir()

    assert tree_digest_from_dir(root) != before


def test_tar_digest_ignores_a_leading_dot_slash_and_the_root_entry(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    dotted = tmp_path / "dotted.tar"
    with tarfile.open(dotted, "w") as archive:
        archive.add(root, arcname=".")

    assert tree_digest_from_tar(dotted) == tree_digest_from_dir(root)


def test_a_fifo_in_a_bundled_folder_refuses_by_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    os.mkfifo(root / "pipe")

    with pytest.raises(MigrationRefused) as refused:
        tree_digest_from_dir(root)
    with pytest.raises(MigrationRefused):
        build_folder_tar(root, tmp_path / "out.tar")

    assert refused.value.reason == "unsupported_file_type"
    assert refused.value.details["path"].endswith("pipe")


def test_extract_refuses_a_member_escaping_the_destination(tmp_path: Path) -> None:
    evil = tmp_path / "evil.tar"
    payload = tmp_path / "payload"
    payload.write_text("x", encoding="utf-8")
    with tarfile.open(evil, "w") as archive:
        archive.add(payload, arcname="../escaped")
    destination = tmp_path / "dest"
    destination.mkdir()

    with pytest.raises(MigrationRefused) as refused:
        extract_tar(evil, destination)

    assert refused.value.reason == "bundle_member_unsafe"
    assert not (tmp_path / "escaped").exists()


def test_sha256_file_is_the_hex_digest_of_the_bytes(tmp_path: Path) -> None:
    target = tmp_path / "f"
    target.write_bytes(b"abc")

    assert (
        sha256_file(target)
        == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_the_folder_tar_builder_itself_leaves_a_secret_out(tmp_path: Path) -> None:
    """Even a caller that skipped preflight cannot write a tar carrying a
    secret: the builder drops it, and the digest is of what was written."""
    root = tmp_path / "root"
    (root / "lean-sidecar").mkdir(parents=True)
    (root / "lean-sidecar" / ".launcher-token").write_text("live", encoding="utf-8")
    (root / "lean-sidecar" / "state.json").write_text("{}", encoding="utf-8")
    archive = tmp_path / "out.tar"

    build_folder_tar(root, archive)

    with tarfile.open(archive) as written:
        assert sorted(written.getnames()) == ["lean-sidecar", "lean-sidecar/state.json"]
    restored = tmp_path / "restored"
    restored.mkdir()
    extract_tar(archive, restored)
    assert tree_digest_from_dir(restored) == tree_digest_from_tar(archive)


@pytest.mark.parametrize(
    "target", ["/etc/hosts", "../outside", "sub/../../outside"], ids=["absolute", "up", "nested_up"]
)
def test_a_symlink_the_safe_extraction_would_refuse_is_refused_at_the_source(
    tmp_path: Path, target: str
) -> None:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    os.symlink(target, root / "link")

    with pytest.raises(MigrationRefused) as refused:
        require_bundleable({"root": root})
    with pytest.raises(MigrationRefused):
        build_folder_tar(root, tmp_path / "out.tar")

    assert refused.value.reason == "unsafe_symlink_in_bundle_source"
    assert refused.value.details["paths"] == [str(root / "link")]


def test_a_symlink_inside_the_folder_is_bundleable(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.txt").write_text("a", encoding="utf-8")
    os.symlink("sub/a.txt", root / "latest")
    os.symlink("../latest", root / "sub" / "back")

    require_bundleable({"root": root})


def test_a_corrupt_tar_is_a_refusal_not_a_traceback(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "big.bin").write_bytes(os.urandom(64 * 1024))
    archive = tmp_path / "folder.tar"
    build_folder_tar(root, archive)
    truncated = tmp_path / "truncated.tar"
    truncated.write_bytes(archive.read_bytes()[:10_000])
    destination = tmp_path / "dest"
    destination.mkdir()

    with pytest.raises(MigrationRefused) as unpacked:
        extract_tar(truncated, destination)
    with pytest.raises(MigrationRefused) as digested:
        tree_digest_from_tar(truncated)

    assert unpacked.value.reason == "bundle_member_unreadable"
    assert digested.value.reason == "bundle_member_unreadable"
