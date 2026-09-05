"""Executable identity (PRD #1926 review F16)."""

from __future__ import annotations

from pathlib import Path

from app.research.sweep.identity import (
    CodeIdentity,
    environment_digest,
    resolve_code_identity,
    source_digest,
    tree_state,
)


def _service_tree(root: Path) -> Path:
    (root / "app" / "engine").mkdir(parents=True)
    (root / "app" / "engine" / "engine.py").write_text("x = 1\n")
    (root / "app" / "engine" / "__pycache__").mkdir()
    (root / "app" / "engine" / "__pycache__" / "engine.cpython-312.pyc").write_bytes(b"\x00")
    (root / "requirements-light.txt").write_text("pandas==2.0\n")
    return root


def test_same_source_at_the_same_label_with_changed_bytes_has_a_different_digest(tmp_path: Path) -> None:
    root = _service_tree(tmp_path)
    before = source_digest(root, ("app/engine",))

    (root / "app" / "engine" / "engine.py").write_text("x = 2\n")

    assert source_digest(root, ("app/engine",)) != before


def test_bytecode_caches_and_path_order_do_not_move_the_digest(tmp_path: Path) -> None:
    root = _service_tree(tmp_path)
    (root / "app" / "research").mkdir()
    (root / "app" / "research" / "a.py").write_text("a = 1\n")
    one = source_digest(root, ("app/engine", "app/research"))

    (root / "app" / "engine" / "__pycache__" / "engine.cpython-312.pyc").write_bytes(b"\x01\x02")
    other_order = source_digest(root, ("app/research", "app/engine"))

    assert one == other_order


def test_environment_digest_tracks_the_pinned_requirements(tmp_path: Path) -> None:
    root = _service_tree(tmp_path)
    before = environment_digest(root, ("requirements-light.txt",))

    (root / "requirements-light.txt").write_text("pandas==2.1\n")

    assert environment_digest(root, ("requirements-light.txt",)) != before


def test_tree_state_is_unknown_outside_a_git_checkout(tmp_path: Path) -> None:
    assert tree_state(_service_tree(tmp_path), ("app/engine",)) == "unknown"


def test_matches_compares_digests_not_labels() -> None:
    a = CodeIdentity(git_revision="abc", tree_state="clean", source_digest="s", environment_digest="e")
    b = CodeIdentity(git_revision="def", tree_state="unknown", source_digest="s", environment_digest="e")
    c = CodeIdentity(git_revision="abc", tree_state="clean", source_digest="t", environment_digest="e")

    assert a.matches(b)
    assert not a.matches(c)


def test_the_real_service_resolves_a_complete_identity() -> None:
    identity = resolve_code_identity()

    assert len(identity.source_digest) == 64
    assert len(identity.environment_digest) == 64
    assert identity.tree_state in ("clean", "dirty", "unknown")
    assert identity.as_dict()["git_revision"] == identity.git_revision
