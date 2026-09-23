"""The real podman adapter: argument lists, timeouts, loud failures (#2268).

Driven by a scripted runner — no podman is ever invoked. What is pinned is
the exact command each operation issues (no shell, never ``--force``), that
every call carries a timeout, and that a failed or unreadable call is a
refusal naming the volume or container.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.installation_migration.errors import MigrationRefused
from app.installation_migration.podman import SubprocessPodman, VolumeInfo


class _ScriptedRun:
    def __init__(self, *answers: tuple[int, str, str]) -> None:
        self._answers = list(answers)
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, dict(kwargs)))
        code, stdout, stderr = self._answers.pop(0)
        return subprocess.CompletedProcess(command, code, stdout, stderr)


def test_volume_info_reads_driver_and_labels() -> None:
    run = _ScriptedRun(
        (0, "", ""),
        (0, json.dumps([{"Name": "learn-ai_pgdata", "Driver": "local", "Labels": {"a": "b"}}]), ""),
    )

    info = SubprocessPodman(run=run).volume_info("learn-ai_pgdata")

    assert info == VolumeInfo(name="learn-ai_pgdata", driver="local", labels={"a": "b"})
    assert run.calls[0][0] == ["podman", "volume", "exists", "learn-ai_pgdata"]
    assert all("timeout" in kwargs and kwargs["timeout"] for _, kwargs in run.calls)
    assert all(kwargs.get("shell") is None for _, kwargs in run.calls)


def test_volume_info_of_an_absent_volume_is_none() -> None:
    run = _ScriptedRun((1, "", ""))

    assert SubprocessPodman(run=run).volume_info("nope") is None


def test_create_volume_carries_every_label_and_the_driver() -> None:
    run = _ScriptedRun((0, "", ""))

    SubprocessPodman(run=run).create_volume(
        VolumeInfo(name="v", driver="local", labels={"z": "1", "a": "2"})
    )

    assert run.calls[0][0] == [
        "podman", "volume", "create", "--driver", "local",
        "--label", "a=2", "--label", "z=1", "v",
    ]


def test_remove_volume_never_forces() -> None:
    run = _ScriptedRun((0, "", ""))

    SubprocessPodman(run=run).remove_volume("v")

    assert run.calls[0][0] == ["podman", "volume", "rm", "v"]


def test_export_and_import_use_the_transfer_timeout(tmp_path: Path) -> None:
    run = _ScriptedRun((0, "", ""), (0, "", ""))
    podman = SubprocessPodman(run=run, transfer_timeout_s=42.0)

    podman.export_volume("v", tmp_path / "v.tar")
    podman.import_volume("v", tmp_path / "v.tar")

    assert run.calls[0][0] == ["podman", "volume", "export", "--output", str(tmp_path / "v.tar"), "v"]
    assert run.calls[1][0] == ["podman", "volume", "import", "v", str(tmp_path / "v.tar")]
    assert [kwargs["timeout"] for _, kwargs in run.calls] == [42.0, 42.0]


def test_containers_using_volume_reports_running_state() -> None:
    run = _ScriptedRun(
        (
            0,
            json.dumps(
                [
                    {"Names": ["my-postgres"], "State": "running"},
                    {"Names": ["old-one"], "State": "exited"},
                ]
            ),
            "",
        )
    )

    uses = SubprocessPodman(run=run).containers_using_volume("learn-ai_pgdata")

    assert [(use.name, use.running) for use in uses] == [
        ("my-postgres", True),
        ("old-one", False),
    ]


def test_a_failed_command_refuses_naming_the_subject_and_stderr() -> None:
    run = _ScriptedRun((125, "", "Error: volume is being used"))

    with pytest.raises(MigrationRefused) as refused:
        SubprocessPodman(run=run).remove_volume("learn-ai_pgdata")

    assert refused.value.reason == "podman_command_failed"
    assert refused.value.details["subject"] == "learn-ai_pgdata"
    assert "being used" in refused.value.message


def test_a_timed_out_command_refuses() -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])  # type: ignore[arg-type]

    with pytest.raises(MigrationRefused) as refused:
        SubprocessPodman(run=run).stop_container("my-postgres")

    assert refused.value.reason == "podman_command_timed_out"


def test_a_missing_executable_refuses() -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(command[0])

    with pytest.raises(MigrationRefused) as refused:
        SubprocessPodman(run=run).volume_info("v")

    assert refused.value.reason == "podman_unavailable"


_SYSTEM_DF = """Images space usage:

REPOSITORY  TAG  IMAGE ID  CREATED  SIZE  SHARED SIZE  UNIQUE SIZE  CONTAINERS

Local Volumes space usage:

VOLUME NAME                          LINKS       SIZE
learn-ai_pgdata                      1           106.2MB
learn-ai-alpaca-clerk-data           1           2.5GB
learn-ai_alpaca-fleet-control        1           24.58kB
empty-volume                         0           0B
"""


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("learn-ai_pgdata", 106_200_000),
        ("learn-ai-alpaca-clerk-data", 2_500_000_000),
        ("learn-ai_alpaca-fleet-control", 24_580),
        ("empty-volume", 0),
        ("not-listed", None),
    ],
)
def test_volume_size_reads_podman_system_df(name: str, expected: int | None) -> None:
    run = _ScriptedRun((0, _SYSTEM_DF, ""))

    assert SubprocessPodman(run=run).volume_size_bytes(name) == expected
    assert run.calls[0][0] == ["podman", "system", "df", "--verbose"]


def test_an_unreadable_volume_size_is_unknown_not_a_guess() -> None:
    run = _ScriptedRun((0, "Local Volumes space usage:\n\nVOLUME NAME LINKS SIZE\nv 1 lots\n", ""))

    assert SubprocessPodman(run=run).volume_size_bytes("v") is None
