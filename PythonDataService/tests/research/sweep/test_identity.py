"""Executable identity (PRD #1926 review F16; #2588)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import numpy
import pytest

from app.research.sweep import identity as identity_module
from app.research.sweep.identity import (
    DIGEST_SCHEME,
    LEGACY_DIGEST_SCHEME,
    CodeIdentity,
    EnvironmentIdentityError,
    environment_digest,
    installed_distributions,
    resolve_code_identity,
    source_digest,
    tree_state,
)


def _service_tree(root: Path) -> Path:
    (root / "app" / "engine").mkdir(parents=True)
    (root / "app" / "engine" / "engine.py").write_text("x = 1\n")
    (root / "app" / "engine" / "__pycache__").mkdir()
    (root / "app" / "engine" / "__pycache__" / "engine.cpython-312.pyc").write_bytes(b"\x00")
    return root


def _install(site: Path, name: str, version: str) -> Path:
    """A real ``.dist-info`` directory, as pip leaves one, read back through ``importlib.metadata``."""
    info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    return info


def _site(root: Path, *installed: tuple[str, str]) -> list[str]:
    for name, version in installed:
        _install(root, name, version)
    return [str(root)]


# ── Source ───────────────────────────────────────────────────────────────


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


def test_a_test_directory_at_any_depth_is_not_part_of_the_source(tmp_path: Path) -> None:
    """A test cannot change a figure, so editing one must not strand an in-flight study (#2588)."""
    root = _service_tree(tmp_path / "tests" / "service")  # a root that itself sits under a `tests` directory still hashes
    for nested in ("app/engine/tests", "app/engine/strategy/spec/tests"):
        (root / nested).mkdir(parents=True)
        (root / nested / "test_math.py").write_text("assert True\n")
    before = source_digest(root, ("app/engine",))

    (root / "app" / "engine" / "tests" / "test_math.py").write_text("assert 1 == 1\n")
    (root / "app" / "engine" / "strategy" / "spec" / "tests" / "test_new.py").write_text("x = 3\n")

    assert source_digest(root, ("app/engine",)) == before
    (root / "app" / "engine" / "engine.py").write_text("x = 2\n")
    assert source_digest(root, ("app/engine",)) != before


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args], cwd=root, check=True, capture_output=True)


def test_tree_state_ignores_the_test_directories_the_digest_ignores(tmp_path: Path) -> None:
    root = _service_tree(tmp_path)
    (root / "app" / "engine" / "tests").mkdir()
    (root / "app" / "engine" / "tests" / "test_math.py").write_text("assert True\n")
    (root / ".gitignore").write_text("__pycache__/\n")
    _git(root, "init", "-q")
    _git(root, "add", ".gitignore", "app/engine/engine.py", "app/engine/tests/test_math.py")
    _git(root, "commit", "-q", "-m", "seed")

    (root / "app" / "engine" / "tests" / "test_math.py").write_text("assert 1 == 1\n")
    (root / "app" / "engine" / "tests" / "test_untracked.py").write_text("x = 1\n")
    assert tree_state(root, ("app/engine",)) == "clean"

    (root / "app" / "engine" / "engine.py").write_text("x = 2\n")
    assert tree_state(root, ("app/engine",)) == "dirty"


def test_tree_state_is_unknown_outside_a_git_checkout(tmp_path: Path) -> None:
    assert tree_state(_service_tree(tmp_path), ("app/engine",)) == "unknown"


# ── Environment ──────────────────────────────────────────────────────────


def test_the_environment_is_the_installed_versions_not_a_requirement_file(tmp_path: Path) -> None:
    """A transitive or unpinned upgrade on a rebuild moves the digest; nothing else about the site does."""
    site = _site(tmp_path, ("numpy", "2.1.0"), ("some-transitive-dep", "1.0"))
    before = environment_digest(site)
    (tmp_path / "requirements-light.txt").write_text("# a comment edit\n")

    assert environment_digest(site) == before  # stable across calls, deaf to requirement files

    (tmp_path / "some_transitive_dep-1.0.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: some-transitive-dep\nVersion: 1.1\n")
    assert environment_digest(site) != before


def test_the_interpreter_and_the_cpu_are_part_of_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site = _site(tmp_path, ("numpy", "2.1.0"))
    before = environment_digest(site)

    monkeypatch.setattr(identity_module.platform, "machine", lambda: "a-different-cpu")
    other_cpu = environment_digest(site)
    monkeypatch.setattr(identity_module.sys, "version", "3.12.99 (a different build)")
    other_interpreter = environment_digest(site)

    assert len({before, other_cpu, other_interpreter}) == 3


def test_name_spelling_and_search_order_do_not_move_the_environment(tmp_path: Path) -> None:
    """Installers spell a project's name differently; ``sys.path`` order is not an installed fact."""
    first, second = tmp_path / "first", tmp_path / "second"
    _install(first, "typing_extensions", "4.12.2")
    _install(second, "Typing.Extensions", "4.12.2")
    _install(second, "pandas", "2.2.3")

    assert installed_distributions([str(first), str(second)]) == (("pandas", "2.2.3"), ("typing-extensions", "4.12.2"))
    assert environment_digest([str(first), str(second)]) == environment_digest([str(second), str(first)])


def test_a_project_installed_twice_contributes_both_copies(tmp_path: Path) -> None:
    """Whichever copy imports, an upgrade of it moves the digest."""
    first, second = tmp_path / "first", tmp_path / "second"
    _install(first, "numpy", "2.1.0")
    shadowed = _install(second, "numpy", "1.26.4")
    paths: Sequence[str] = [str(first), str(second)]
    before = environment_digest(paths)

    (shadowed / "METADATA").write_text("Metadata-Version: 2.1\nName: numpy\nVersion: 2.0.0\n")

    assert environment_digest(paths) != before


def test_an_unreadable_distribution_refuses_instead_of_digesting_around_it(tmp_path: Path) -> None:
    broken = tmp_path / "broken-1.0.dist-info"
    broken.mkdir()
    (broken / "METADATA").write_text("Metadata-Version: 2.1\nName: broken\n")

    with pytest.raises(EnvironmentIdentityError, match=r"'broken'.*no version"):
        environment_digest([str(tmp_path)])


def test_an_environment_with_nothing_installed_refuses(tmp_path: Path) -> None:
    with pytest.raises(EnvironmentIdentityError, match="no installed distribution"):
        environment_digest([str(tmp_path)])


def test_the_real_environment_reports_the_numpy_that_imports() -> None:
    assert ("numpy", numpy.__version__) in installed_distributions()


# ── The receipted identity ───────────────────────────────────────────────


def test_a_receipt_written_before_digest_schemes_reads_back_as_the_legacy_scheme() -> None:
    """Receipts from before #2588 carry no ``digest_scheme``: their digests hashed requirement-file bytes."""
    legacy = {"git_revision": "abc", "tree_state": "clean", "source_digest": "s" * 64, "environment_digest": "e" * 64}

    assert CodeIdentity.from_dict(legacy).digest_scheme == LEGACY_DIGEST_SCHEME
    assert CodeIdentity.from_dict({**legacy, "digest_scheme": DIGEST_SCHEME}).digest_scheme == DIGEST_SCHEME
    with pytest.raises(TypeError):
        CodeIdentity(**legacy)  # only from_dict may assume the legacy scheme


def test_the_real_service_resolves_a_complete_identity() -> None:
    identity = resolve_code_identity()

    assert len(identity.source_digest) == 64
    assert len(identity.environment_digest) == 64
    assert identity.digest_scheme == DIGEST_SCHEME
    assert identity.tree_state in ("clean", "dirty", "unknown")
    assert CodeIdentity.from_dict(identity.as_dict()) == identity
