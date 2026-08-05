"""Shared account-scoped path safety — used by the JSONL journal and the
SQLite clerk repository. Exercises the containment sanitiser and the
fsync_directory_chain fix for an unresolved-root-vs-resolved-descendant
mismatch (the exact class of bug that caused an infinite loop on macOS,
where a caller's raw ``/var/folders/...`` tmp path resolves to
``/private/var/folders/...``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.paths import (
    fsync_directory_chain,
    resolve_contained_path,
    safe_path_component,
)


def test_safe_path_component_accepts_ordinary_account_ids() -> None:
    assert safe_path_component("PA-284968", "account_id") == "PA-284968"


@pytest.mark.parametrize("value", ["..", ".", "../escape", "a/b", "a\\b", ""])
def test_safe_path_component_rejects_traversal_and_separators(value: str) -> None:
    with pytest.raises(ValueError):
        safe_path_component(value, "account_id")


def test_resolve_contained_path_stays_inside_root(tmp_path: Path) -> None:
    resolved = resolve_contained_path(tmp_path, "accounts", "alpaca", "PA-1")
    assert resolved == (tmp_path / "accounts" / "alpaca" / "PA-1").resolve()


def test_resolve_contained_path_rejects_a_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-target"
    outside.mkdir(exist_ok=True)
    root = tmp_path / "root"
    root.mkdir()
    escape = root / "escape"
    escape.symlink_to(outside)

    with pytest.raises(ValueError):
        resolve_contained_path(root, "escape")


def test_fsync_directory_chain_reaches_a_symlinked_unresolved_root(tmp_path: Path) -> None:
    """Regression test: the caller passes an *unresolved* root (as every real
    caller does — Path objects from tempfile/config are not pre-resolved),
    while the starting directory is already realpath-resolved (as
    resolve_contained_path always returns). Before the fix, comparing an
    unresolved root against a resolved descendant never matched, walking
    past "/" and spinning forever. Proving this returns at all (within the
    test's normal timeout) is the regression test; a hang is the failure
    mode being guarded against.
    """
    real_root = tmp_path / "real_root"
    real_root.mkdir()
    nested = real_root / "accounts" / "alpaca" / "PA-1"
    nested.mkdir(parents=True)

    symlinked_root = tmp_path / "symlinked_root"
    symlinked_root.symlink_to(real_root)
    unresolved_root = symlinked_root  # deliberately not .resolve()'d

    fsync_directory_chain(nested, unresolved_root)  # must return, not hang


def test_fsync_directory_chain_raises_rather_than_looping_when_root_is_unreachable(
    tmp_path: Path,
) -> None:
    unrelated_root = tmp_path / "unrelated"
    unrelated_root.mkdir()
    nested = tmp_path / "nested"
    nested.mkdir()

    with pytest.raises(ValueError):
        fsync_directory_chain(nested, unrelated_root)
