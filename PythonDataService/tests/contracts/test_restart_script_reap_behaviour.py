"""Behavioural guard on the one command in this repo that runs ``podman rm -f``.

`restart.sh` decides, for every container stuck in ``Created``, whether to
destroy it or restart it. A text-matching contract test cannot answer the only
question that matters — *which names actually reach ``rm -f``* — and provably
does not: an earlier revision of this script classified orphans as "created and
not in project X", which deletes the whole stack whenever the derived project
name is wrong, and the source-level assertions passed against it.

So this drives the real script against a fake ``podman`` and asserts on the
calls it makes. The fake models container state (status, labels, health) and
mutates it on ``start``, so the script's recovery path runs end to end.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
RESTART_SCRIPT = REPOSITORY_ROOT / "restart.sh"

_PROJECT = "learn-ai"

# Two compose services: one healthy, one abandoned in Created by a slow
# `depends_on: service_healthy` gate. Plus one genuinely orphaned probe.
_FIXTURE = [
    {"name": "polygon-data-service", "status": "running", "project": _PROJECT,
     "service": "python-service", "healthy": True},
    {"name": "alpaca-live-clerk", "status": "created", "project": _PROJECT,
     "service": "alpaca-live-clerk", "healthy": False},
    {"name": "sleep-probe-9", "status": "created", "project": None,
     "service": None, "healthy": False},
]

_FAKE_PODMAN = r'''#!/usr/bin/env python3
import json, os, sys
state_path = os.environ["FAKE_PODMAN_STATE"]
calls_path = os.environ["FAKE_PODMAN_CALLS"]
args = sys.argv[1:]

with open(state_path) as handle:
    containers = json.load(handle)
with open(calls_path) as handle:
    calls = json.load(handle)


def save():
    with open(state_path, "w") as handle:
        json.dump(containers, handle)
    with open(calls_path, "w") as handle:
        json.dump(calls, handle)


def flag(name):
    return [args[i + 1] for i, a in enumerate(args) if a == name and i + 1 < len(args)]


if args[:1] == ["compose"]:
    calls.setdefault("compose", []).append(args[1:])
    save()
    if "config" in args and "--services" in args:
        print("\n".join(sorted({c["service"] for c in containers if c["service"]})))
    sys.exit(0)

if args[:1] == ["rm"]:
    targets = [a for a in args[1:] if not a.startswith("-")]
    calls.setdefault("rm", []).extend(targets)
    containers[:] = [c for c in containers if c["name"] not in targets]
    save(); sys.exit(0)

if args[:1] == ["start"]:
    targets = [a for a in args[1:] if not a.startswith("-")]
    calls.setdefault("start", []).extend(targets)
    for c in containers:
        if c["name"] in targets:
            c["status"] = "running"; c["healthy"] = True
    save(); sys.exit(0)

if args[:1] == ["ps"]:
    rows = list(containers)
    show_all = "-a" in args
    if not show_all:
        rows = [c for c in rows if c["status"] == "running"]
    for f in flag("--filter"):
        if f == "status=created":
            rows = [c for c in rows if c["status"] == "created"]
        elif f == "health=healthy":
            rows = [c for c in rows if c["healthy"]]
        elif f.startswith("label!="):
            key = f.split("=", 1)[1]
            if key == "com.docker.compose.project":
                rows = [c for c in rows if c["project"] is None]
        elif f.startswith("label="):
            spec = f.split("=", 1)[1]
            if "=" in spec:
                _, value = spec.split("=", 1)
                rows = [c for c in rows if c["project"] == value]
            else:
                rows = [c for c in rows if c["project"] is not None]
    fmt = (flag("--format") or ["{{.Names}}"])[0]
    for c in rows:
        if "com.docker.compose.service" in fmt:
            print(c["service"] or "")
        elif fmt.startswith("table"):
            print(c["name"])
        else:
            print(c["name"])
    sys.exit(0)

sys.exit(0)
'''


def _fake_podman_environment(
    tmp_path: Path, project_name: str, extra_env: dict[str, str] | None = None,
) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "podman"
    fake.write_text(_FAKE_PODMAN, encoding="utf-8")
    fake.chmod(0o755)

    state = tmp_path / "state.json"
    calls = tmp_path / "calls.json"
    state.write_text(json.dumps(_FIXTURE), encoding="utf-8")
    calls.write_text("{}", encoding="utf-8")

    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_PODMAN_STATE": str(state),
        "FAKE_PODMAN_CALLS": str(calls),
        "COMPOSE_PROJECT_NAME": project_name,
        # Drive the verdict without waiting the production poll budget.
        "RESTART_HEALTH_ATTEMPTS": "3",
        "RESTART_HEALTH_INTERVAL": "0",
        **(extra_env or {}),
    }
    return environment, calls


def _run(tmp_path: Path, project_name: str) -> dict[str, list[str]]:
    environment, calls = _fake_podman_environment(tmp_path, project_name)
    # The reap happens long before the health wait, so its record is already on
    # disk even if the script never settles. A script that never reaches a
    # verdict must still be judged on what it destroyed.
    with contextlib.suppress(subprocess.TimeoutExpired):
        subprocess.run(
            ["bash", str(RESTART_SCRIPT)],
            capture_output=True, text=True, env=environment,
            cwd=REPOSITORY_ROOT, timeout=60,
        )
    # The exit status is deliberately not asserted: the wrong-project scenario
    # can never reach a healthy verdict, and what this test guards is which
    # containers were destroyed, not whether the stack came up.
    return json.loads(calls.read_text(encoding="utf-8"))


def _run_capturing(
    tmp_path: Path, argv: list[str] | None = None, extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, list[str]]]:
    """Like `_run`, but also returns the process result. Tests asserting on
    argument handling (exit code, usage text, an unbound-variable crash) need
    the result itself, not only which containers were touched."""
    environment, calls = _fake_podman_environment(tmp_path, _PROJECT, extra_env)
    completed = subprocess.run(
        ["bash", str(RESTART_SCRIPT), *(argv or [])],
        capture_output=True, text=True, env=environment,
        cwd=REPOSITORY_ROOT, timeout=60,
    )
    return completed, json.loads(calls.read_text(encoding="utf-8"))


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required to run restart.sh")
def test_a_stuck_clerk_is_restarted_and_never_destroyed(tmp_path: Path) -> None:
    calls = _run(tmp_path, _PROJECT)

    assert "alpaca-live-clerk" not in calls.get("rm", []), (
        "A compose-managed clerk stuck in Created must never be reaped. This is "
        "the bug that destroyed a live execution lane on a routine restart."
    )
    assert "alpaca-live-clerk" in calls.get("start", []), (
        "A compose-managed container stuck in Created must be restarted."
    )
    assert calls.get("rm", []) == ["sleep-probe-9"], (
        f"Only the genuinely orphaned probe may be destroyed; got {calls.get('rm')}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required to run restart.sh")
def test_a_wrong_project_name_can_never_destroy_compose_containers(tmp_path: Path) -> None:
    """The catastrophic case, which a source-level assertion cannot see.

    Framing orphans as "created and NOT in project X" deletes every compose
    container the moment the derived project name is wrong. Framing them as
    "created and carrying no compose project label" cannot, whatever the name.
    """
    calls = _run(tmp_path, "a-name-that-matches-nothing")

    destroyed = set(calls.get("rm", []))
    assert "alpaca-live-clerk" not in destroyed, (
        "A wrong project name reaped a compose-managed clerk. The destructive "
        "query must test for ABSENCE of the compose label, never for a "
        "mismatch against our own project name."
    )
    assert destroyed <= {"sleep-probe-9"}, (
        f"A wrong project name destroyed compose-managed containers: {destroyed}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required to run restart.sh")
def test_restart_passes_the_committed_fleet_overlay_to_every_compose_call(tmp_path: Path) -> None:
    """A committed overlay that restart.sh does not pass is a topology that
    does not run. Compose auto-loads compose.override.yaml only, so the
    committed compose.fleet.dev.yaml (the fenced role split) must be named
    explicitly on every single compose invocation, not just some of them —
    a missed spot silently reverts that one operation to the unfenced
    `combined` posture.
    """
    calls = _run(tmp_path, _PROJECT)

    compose_calls = calls.get("compose", [])
    assert compose_calls, "restart.sh made no compose call"
    for call in compose_calls:
        assert call.count("--file") >= 2, call
        assert any(arg.endswith("compose.fleet.dev.yaml") for arg in call), call


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required to run restart.sh")
def test_help_flag_exits_zero_and_touches_no_compose_call(tmp_path: Path) -> None:
    """The incident this guards against: `./restart.sh --help`, before `--help`
    was a recognised flag, fell straight through to the teardown path. Only an
    unrelated SIGPIPE from a piped consumer aborted it before `podman compose
    down` ran against two live broker clerks — that was luck, not a guarantee.
    """
    completed, calls = _run_capturing(tmp_path, argv=["--help"])

    assert completed.returncode == 0
    assert not calls.get("compose"), f"--help must touch no compose command; got {calls.get('compose')}"
    assert "Usage" in completed.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required to run restart.sh")
def test_short_help_flag_also_exits_zero_and_touches_no_compose_call(tmp_path: Path) -> None:
    completed, calls = _run_capturing(tmp_path, argv=["-h"])

    assert completed.returncode == 0
    assert not calls.get("compose")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required to run restart.sh")
def test_an_unrecognised_argument_is_refused_before_touching_compose(tmp_path: Path) -> None:
    """Any argument the script does not recognise must be refused before its
    very first destructive action (`podman compose down`), never fall through
    to it — the exact shape of the `--help` incident above, generalised."""
    completed, calls = _run_capturing(tmp_path, argv=["--bogus"])

    assert completed.returncode != 0
    assert not calls.get("compose"), (
        f"an unrecognised argument reached a compose command: {calls.get('compose')}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required to run restart.sh")
def test_an_explicit_compose_file_env_var_does_not_crash_under_set_u(tmp_path: Path) -> None:
    """`COMPOSE_FILE` set leaves `COMPOSE_ARGS` an empty array (Compose's own
    documented override takes over entirely). This host's `/usr/bin/env bash`
    is 3.2 (macOS default), where `set -u` treats a bare `"${ARR[@]}"` on an
    empty array as an unbound-variable error — every `podman compose` call
    site must use the bash-3.2-safe `${ARR[@]+"${ARR[@]}"}` idiom instead.
    """
    completed, calls = _run_capturing(tmp_path, extra_env={"COMPOSE_FILE": "compose.yaml"})

    assert "unbound variable" not in completed.stderr, completed.stderr
    assert calls.get("compose"), "restart.sh made no compose call under COMPOSE_FILE"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required to run restart.sh")
def test_a_missing_service_is_never_reported_as_a_healthy_stack(tmp_path: Path) -> None:
    """`podman ps` lists running containers only, so a crashed service is absent
    from BOTH the healthy count and the total and the ratio stays balanced. The
    verdict must therefore compare against what compose declares, or it reports
    success with a live execution lane simply gone.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "podman"
    fake.write_text(_FAKE_PODMAN, encoding="utf-8")
    fake.chmod(0o755)

    # `alpaca-live-clerk` is declared by compose but is not running at all —
    # it did not merely fail its healthcheck, it is absent.
    state = tmp_path / "state.json"
    calls = tmp_path / "calls.json"
    state.write_text(json.dumps([
        {"name": "polygon-data-service", "status": "running", "project": _PROJECT,
         "service": "python-service", "healthy": True},
        {"name": "alpaca-live-clerk", "status": "exited", "project": _PROJECT,
         "service": "alpaca-live-clerk", "healthy": False},
    ]), encoding="utf-8")
    calls.write_text("{}", encoding="utf-8")

    completed = subprocess.run(
        ["bash", str(RESTART_SCRIPT)],
        capture_output=True, text=True, timeout=60, cwd=REPOSITORY_ROOT,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
             "FAKE_PODMAN_STATE": str(state), "FAKE_PODMAN_CALLS": str(calls),
             "COMPOSE_PROJECT_NAME": _PROJECT,
             "RESTART_HEALTH_ATTEMPTS": "2", "RESTART_HEALTH_INTERVAL": "0"},
    )

    assert completed.returncode != 0, (
        "restart.sh reported success while a declared service was not running. "
        f"stdout:\n{completed.stdout[-2000:]}"
    )
    assert "alpaca-live-clerk" in completed.stdout, (
        "The operator must be told which declared service is missing."
    )
    assert "All 1 services healthy" not in completed.stdout
