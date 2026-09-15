"""Contract: the Paper-to-Live handoff script never hardcodes a compose target.

``alpaca-paper-to-live-handoff.sh`` used to call ``podman compose <verb>
python-service`` directly at five call sites (stop, the dev-reset run, start,
the control-secret exec, and the final restart). ``python-service`` is the
coordinator on the dev two-lane posture and the fleet overlays, not a lane
worker — restarting it does not restart either lane (#2147), exactly the bug
that motivated authoring the desk's own restart command from the deployment's
declared ``FLEET_WORKER_SERVICE``
(``PythonDataService/app/broker_configuration/desk_state.py``'s
``worker_restart_command`` / ``runtime.py``'s ``worker_restart_target_from``,
#2138). This pins the script's own derivation, mirrored in bash because a
shell asset cannot import the Python desk module.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
HANDOFF_SCRIPT = (
    REPOSITORY_ROOT / "Frontend" / "src" / "assets" / "scripts" / "alpaca-paper-to-live-handoff.sh"
)


def _script() -> str:
    return HANDOFF_SCRIPT.read_text(encoding="utf-8")


def test_no_compose_verb_hardcodes_python_service() -> None:
    """Every ``podman compose <verb>`` call site targets the derived variable."""
    script = _script()
    for verb in ("stop", "start", "restart", "run --rm --no-deps", "exec -T"):
        assert f"{verb} python-service" not in script, (
            f"'podman compose {verb} python-service' is hardcoded; it must use "
            '"$worker_service" (or "${compose_words[@]}"), derived from '
            "FLEET_WORKER_SERVICE, the same way the desk authors its own "
            "restart command."
        )

    # The one legitimate remaining functional reference: the combined-posture
    # fallback, when FLEET_WORKER_SERVICE is undeclared (comments may still
    # name python-service in prose explaining the coordinator distinction).
    non_comment_lines = [
        line for line in script.splitlines() if not line.strip().startswith("#")
    ]
    functional_references = [
        line for line in non_comment_lines if "python-service" in line
    ]
    assert functional_references == ['worker_service="${worker_service:-python-service}"'], (
        f"unexpected non-comment reference(s) to python-service: {functional_references}"
    )


def _run_header(env: dict[str, str], cwd: Path) -> dict[str, str]:
    """Drive the script's real derivation logic (through the compose_words
    build), against a fake repo root, and report what it resolved.

    Slices out everything before the first interactive `read`, so this never
    blocks on stdin, and appends a probe that prints the two variables every
    downstream `podman compose` call site consumes.
    """
    lines = _script().splitlines(keepends=True)
    read_prompt_index = next(
        i for i, line in enumerate(lines) if line.lstrip().startswith("read -r -p")
    )
    header = "".join(lines[:read_prompt_index])
    probe = (
        'printf "WORKER_SERVICE=%s\\n" "$worker_service"\n'
        'printf "COMPOSE_WORDS=%s\\n" "${compose_words[*]}"\n'
    )
    completed = subprocess.run(
        ["bash", "-c", header + probe],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    result: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, _, value = line.partition("=")
        result[key] = value
    return result


def _fake_repo(tmp_path: Path) -> Path:
    (tmp_path / "PythonDataService").mkdir()
    (tmp_path / "compose.yaml").write_text("", encoding="utf-8")
    return tmp_path


def test_derivation_falls_back_to_python_service_when_undeclared(tmp_path: Path) -> None:
    """The combined dev posture (no FLEET_WORKER_SERVICE) is unchanged."""
    resolved = _run_header(env={"PATH": "/usr/bin:/bin"}, cwd=_fake_repo(tmp_path))
    assert resolved["WORKER_SERVICE"] == "python-service"
    assert resolved["COMPOSE_WORDS"] == "podman compose"


def test_derivation_reads_worker_service_from_dot_env(tmp_path: Path) -> None:
    """A deployment's declared FLEET_WORKER_SERVICE overrides the fallback."""
    repo = _fake_repo(tmp_path)
    (repo / ".env").write_text("FLEET_WORKER_SERVICE=alpaca-paper-worker\n", encoding="utf-8")

    resolved = _run_header(env={"PATH": "/usr/bin:/bin"}, cwd=repo)

    assert resolved["WORKER_SERVICE"] == "alpaca-paper-worker"
    assert resolved["COMPOSE_WORDS"] == "podman compose"


def test_derivation_builds_the_full_compose_context_in_order(tmp_path: Path) -> None:
    """Project, then every -f file in order, then profile — matching
    worker_restart_command's own word order (desk_state.py)."""
    repo = _fake_repo(tmp_path)
    (repo / ".env").write_text(
        "FLEET_WORKER_SERVICE=alpaca-live-worker\n"
        "FLEET_COMPOSE_PROJECT=learnai-fleet\n"
        "FLEET_COMPOSE_FILES=compose.yaml,compose.fleet.yaml\n"
        "FLEET_COMPOSE_PROFILE=live\n",
        encoding="utf-8",
    )

    resolved = _run_header(env={"PATH": "/usr/bin:/bin"}, cwd=repo)

    assert resolved["WORKER_SERVICE"] == "alpaca-live-worker"
    assert resolved["COMPOSE_WORDS"] == (
        "podman compose --project-name learnai-fleet "
        "-f compose.yaml -f compose.fleet.yaml --profile live"
    )


def test_a_shell_exported_override_wins_over_dot_env(tmp_path: Path) -> None:
    """Matching compose's own precedent: the shell environment wins."""
    repo = _fake_repo(tmp_path)
    (repo / ".env").write_text("FLEET_WORKER_SERVICE=from-dot-env\n", encoding="utf-8")

    resolved = _run_header(
        env={"PATH": "/usr/bin:/bin", "FLEET_WORKER_SERVICE": "from-shell"}, cwd=repo
    )

    assert resolved["WORKER_SERVICE"] == "from-shell"


def test_derivation_never_aborts_under_set_dash_e_with_no_dot_env(tmp_path: Path) -> None:
    """A missing FLEET_* key (grep finds nothing) must not trip `set -e`.

    This is the regression a naive `grep ... | cut ...` introduces: under
    `pipefail`, grep's no-match exit status kills the whole script the
    instant it resolves the very first key, before the account-id prompt
    ever appears.
    """
    repo = _fake_repo(tmp_path)
    (repo / ".env").write_text("SOME_OTHER_VAR=1\n", encoding="utf-8")

    resolved = _run_header(env={"PATH": "/usr/bin:/bin"}, cwd=repo)

    assert resolved["WORKER_SERVICE"] == "python-service"
