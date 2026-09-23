"""The ``migrate_installation`` operator surface (#2268).

Drives ``scripts.migrate_installation.main`` in-process with the real
adapters swapped for fakes: one JSON line per step, the 0/1/2 exit-code
vocabulary shared with ``manage_broker_fleet``, and a refusal line that
names what it refused.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.installation_migration.bundle import read_manifest
from scripts import migrate_installation
from scripts.migrate_installation import Ports, main
from tests.installation_migration._support import (
    LIVE_ACCOUNT,
    FakeGit,
    build_empty_destination,
    build_installation,
)


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]


def test_export_check_on_an_open_position_exits_2_naming_the_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    installation = build_installation(tmp_path)
    live = installation.lanes.quiet[installation.live_clerk_id]
    installation.lanes.quiet[installation.live_clerk_id] = {**live, "account_flat": False}
    monkeypatch.setattr(
        migrate_installation,
        "build_ports",
        lambda _args: Ports(podman=installation.podman, git=FakeGit(), lanes=installation.lanes),
    )

    code = main(["export", "--check", "--repo-root", str(installation.repo_root)])

    assert code == 2
    refusal = _lines(capsys)[-1]
    assert refusal["reason"] == "accounts_not_flat"
    assert LIVE_ACCOUNT in refusal["error"]
    assert refusal["details"]["accounts"][0]["open"] == ["the account is not flat"]


def test_export_then_import_round_trips_with_exit_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    installation = build_installation(tmp_path)
    bundle = tmp_path / "bundle.tar"
    monkeypatch.setattr(
        migrate_installation,
        "build_ports",
        lambda _args: Ports(podman=installation.podman, git=FakeGit(), lanes=installation.lanes),
    )
    assert (
        main(
            [
                "export",
                "--repo-root", str(installation.repo_root),
                "--bundle", str(bundle),
                "--operator", "inkant",
                "--change-ref", "migrate-2026-09-22",
            ]
        )
        == 0
    )
    exported = _lines(capsys)
    assert exported[-1]["step"] == "complete"

    repo_root, podman = build_empty_destination(tmp_path)
    monkeypatch.setattr(
        migrate_installation,
        "build_ports",
        lambda _args: Ports(podman=podman, git=FakeGit(), lanes=None),
    )
    code = main(
        [
            "import",
            "--repo-root", str(repo_root),
            "--bundle", str(bundle),
            "--aside-dir", str(tmp_path / "aside"),
        ]
    )

    assert code == 0
    imported = _lines(capsys)
    assert [line["step"] for line in imported] == [
        "manifest",
        "code",
        "bundle-verified",
        "identity-verified",
        "host-resolution",
        "moved-aside",
        "restored",
        "destination-verified",
        "complete",
    ]


def test_export_without_a_bundle_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    installation = build_installation(tmp_path)
    monkeypatch.setattr(
        migrate_installation,
        "build_ports",
        lambda _args: Ports(podman=installation.podman, git=FakeGit(), lanes=installation.lanes),
    )

    code = main(["export", "--repo-root", str(installation.repo_root), "--operator", "inkant"])

    assert code == 1
    assert "--bundle" in _lines(capsys)[-1]["error"]
    assert installation.lanes.stopped == []


def test_the_control_secret_comes_from_the_environment_then_the_root_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATA_PLANE_CONTROL_SECRET", raising=False)
    assert migrate_installation._control_secret(tmp_path) is None

    (tmp_path / ".env").write_text('DATA_PLANE_CONTROL_SECRET="from-dotenv"\n', encoding="utf-8")
    assert migrate_installation._control_secret(tmp_path) == "from-dotenv"

    monkeypatch.setenv("DATA_PLANE_CONTROL_SECRET", "from-env")
    assert migrate_installation._control_secret(tmp_path) == "from-env"


def test_export_allow_dirty_tree_is_passed_through_and_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    installation = build_installation(tmp_path)
    bundle = tmp_path / "bundle.tar"
    monkeypatch.setattr(
        migrate_installation,
        "build_ports",
        lambda _args: Ports(
            podman=installation.podman, git=FakeGit(dirty=True), lanes=installation.lanes
        ),
    )
    argv = [
        "export",
        "--repo-root", str(installation.repo_root),
        "--bundle", str(bundle),
        "--operator", "inkant",
        "--change-ref", "migrate-2026-09-22",
    ]

    assert main(argv) == 2
    assert _lines(capsys)[-1]["reason"] == "source_tree_dirty"
    assert main([*argv, "--allow-dirty-tree"]) == 0
    assert read_manifest(bundle).dirty_tree_override is True
