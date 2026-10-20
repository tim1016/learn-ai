"""Destination code must be the manifest's commit or a descendant (#2268).

Ancestry, not time: a real throwaway repository proves the adapter reads
``git merge-base --is-ancestor`` and treats an unknown commit as unknown.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.installation_migration.git import SubprocessGit


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "-c", "commit.gpgsign=false",
            *arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _repo_with_two_commits(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a").write_text("1", encoding="utf-8")
    _git(repo, "add", "a")
    _git(repo, "commit", "-q", "-m", "one")
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "a").write_text("2", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "two")
    second = _git(repo, "rev-parse", "HEAD")
    return repo, first, second


def test_the_adapter_reads_the_commit_graph_and_the_working_tree(tmp_path: Path) -> None:
    repo, first, second = _repo_with_two_commits(tmp_path)
    git = SubprocessGit(repo)

    assert git.head_commit() == second, "head follows the commit graph"
    assert git.is_ancestor(first, second) is True, "the first commit is an ancestor of the second"
    assert git.is_ancestor(second, second) is True, "a commit is its own ancestor"
    assert git.is_ancestor(second, first) is False, "the second commit is not an ancestor of the first"
    assert git.commit_exists("0" * 40) is False, "an unknown commit does not exist"
    assert git.tree_dirty() is False, "a clean tree is not dirty"

    (repo / "a").write_text("3", encoding="utf-8")

    assert git.tree_dirty() is True, "a tracked edit makes the tree dirty"
