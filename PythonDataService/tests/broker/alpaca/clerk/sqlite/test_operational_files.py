"""The file primitives under cutover, ``dev_reset`` and quarantine manifests refuse what they cannot vouch for.

A manifest names an artifact by a root-relative reference and binds it by a
tree hash. A reference that leaves the root, or a tree the hash would follow
through a link, would let a manifest vouch for bytes outside what was
inspected.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.operational_files import (
    confined_relative_path,
    relative_reference,
    tree_sha256,
)

# ── confined_relative_path ───────────────────────────────────────────────────


def test_a_relative_reference_resolves_under_the_root(tmp_path: Path) -> None:
    resolved = confined_relative_path(tmp_path, "legacy/accounts/PA1.json")

    assert resolved == Path(os.path.realpath(tmp_path)) / "legacy" / "accounts" / "PA1.json"


@pytest.mark.parametrize("reference", ["", "."])
def test_an_empty_reference_is_refused_not_resolved_to_the_root(tmp_path: Path, reference: str) -> None:
    with pytest.raises(ValueError, match="non-empty relative path"):
        confined_relative_path(tmp_path, reference)


def test_an_absolute_reference_is_refused_even_when_it_names_a_path_inside_the_root(tmp_path: Path) -> None:
    inside = tmp_path / "legacy" / "accounts"

    with pytest.raises(ValueError, match="non-empty relative path"):
        confined_relative_path(tmp_path, str(inside))


def test_a_reference_that_climbs_out_of_the_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()

    with pytest.raises(ValueError, match="path escapes root"):
        confined_relative_path(root, "../outside.json")


def test_a_reference_through_a_link_that_leaves_the_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="path escapes root"):
        confined_relative_path(root, "link/payload.json")


# ── relative_reference ───────────────────────────────────────────────────────


def test_a_path_under_the_root_is_referenced_by_its_posix_relative_name(tmp_path: Path) -> None:
    assert relative_reference(tmp_path, tmp_path / "legacy" / "accounts" / "PA1.json") == (
        "legacy/accounts/PA1.json"
    )


def test_a_path_outside_the_root_has_no_reference(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()

    with pytest.raises(ValueError, match="outside artifacts root"):
        relative_reference(root, tmp_path / "elsewhere" / "PA1.json")


# ── tree_sha256 ──────────────────────────────────────────────────────────────


def test_a_file_hash_binds_its_name_and_its_bytes(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_bytes(b"{}")
    (tmp_path / "same_bytes_other_name.json").write_bytes(b"{}")
    (tmp_path / "b.json").write_bytes(b'{"x":1}')

    base = tree_sha256(tmp_path / "a.json")

    assert tree_sha256(tmp_path / "a.json") == base
    assert tree_sha256(tmp_path / "same_bytes_other_name.json") != base
    assert tree_sha256(tmp_path / "b.json") != base


def test_a_directory_hash_binds_relative_names_nesting_and_empty_directories(tmp_path: Path) -> None:
    def tree(name: str, files: dict[str, bytes], directories: tuple[str, ...] = ()) -> Path:
        root = tmp_path / name
        root.mkdir()
        for relative, content in files.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        for relative in directories:
            (root / relative).mkdir(parents=True, exist_ok=True)
        return root

    base = tree("base", {"a/one.json": b"1"})
    renamed = tree("renamed", {"a/two.json": b"1"})
    moved = tree("moved", {"b/one.json": b"1"})
    edited = tree("edited", {"a/one.json": b"2"})
    with_empty_directory = tree("with_empty", {"a/one.json": b"1"}, directories=("empty",))

    digests = {
        tree_sha256(base),
        tree_sha256(renamed),
        tree_sha256(moved),
        tree_sha256(edited),
        tree_sha256(with_empty_directory),
    }

    assert len(digests) == 5


def test_a_symbolic_link_artifact_is_refused_rather_than_hashed_through(tmp_path: Path) -> None:
    target = tmp_path / "real.json"
    target.write_bytes(b"{}")
    link = tmp_path / "link.json"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="symbolic-link artifact"):
        tree_sha256(link)


def test_a_symbolic_link_below_a_directory_artifact_is_refused(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "real.json").write_bytes(b"{}")
    (artifact / "link.json").symlink_to(artifact / "real.json")

    with pytest.raises(ValueError, match="symbolic link below legacy artifact"):
        tree_sha256(artifact)


def test_an_entry_that_is_neither_file_nor_directory_below_an_artifact_is_refused(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    os.mkfifo(artifact / "pipe")

    with pytest.raises(ValueError, match="unsupported legacy artifact type"):
        tree_sha256(artifact)


def test_an_artifact_that_is_neither_file_nor_directory_is_refused(tmp_path: Path) -> None:
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)

    with pytest.raises(ValueError, match="not a regular file or directory"):
        tree_sha256(pipe)

    with pytest.raises(ValueError, match="not a regular file or directory"):
        tree_sha256(tmp_path / "missing")
