"""Contract: the Paper-to-Live handoff script never hardcodes a compose
target, never guesses one, and resolves the identical compose invocation the
desk itself authors for the same lane.

``alpaca-paper-to-live-handoff.sh`` used to call ``podman compose <verb>
python-service`` directly at five call sites (stop, the dev-reset run, start,
the control-secret exec, and the final restart). ``python-service`` is the
coordinator on the dev two-lane posture and the fleet overlays, not a lane
worker — restarting it does not restart either lane (#2147), exactly the bug
that motivated authoring the desk's own restart command from the deployment's
declared ``FLEET_WORKER_SERVICE``
(``PythonDataService/app/broker_configuration/desk_state.py``'s
``worker_restart_command`` / ``runtime.py``'s ``worker_restart_target_from``,
#2138).

``FLEET_WORKER_SERVICE`` and its compose-context siblings are declared only
inside compose service environments (``compose.yaml``,
``compose.fleet.dev.yaml``, ``compose.fleet.yaml``) — a repo-root ``.env``
never carries them. The script no longer pretends otherwise: it reads these
four as plain shell-exported variables (never a ``.env`` fallback), requires
``FLEET_WORKER_SERVICE`` with no default, and the Configuration page's
"Copy handoff script" button is what supplies the correct value, by reading
the same desk-authored ``restart_command`` the switch guide renders and
splicing matching ``export`` lines into the copied bytes
(``configuration-handoff-script.component.ts``'s ``withWorkerExports``).

#2158 extended the same posture-awareness to the Configuration HTTP surface.
A fleet lane publishes nothing on the host and carries no
``DATA_PLANE_CONTROL_SECRET`` — only the coordinator does — so the ceremony
reads the coordinator's compose service name from the lane's own
``FLEET_COORDINATOR_URL`` declaration, takes the control secret from that
coordinator, and builds every Configuration URL through one ``config_route``
helper: the coordinator's clerk-scoped fleet catalog route (the same URL the
Configuration page builds via ``operationUrl('configuration_selection_read',
{broker, clerkId})``) when the lane declares a ``FLEET_CLERK_ID``, the
combined posture's direct in-process route otherwise. The tests below pin
that URL to the committed catalog snapshot and execute the script's own
derivation lines — both sides run, per CLAUDE.md guiding philosophy #5 —
rather than pinning a second, hand-maintained expectation that could drift
from the function it mirrors.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.broker_configuration.desk_state import WorkerRestartTarget, worker_restart_command
from app.config import FleetSettings

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
HANDOFF_SCRIPT = (
    REPOSITORY_ROOT / "Frontend" / "src" / "assets" / "scripts" / "alpaca-paper-to-live-handoff.sh"
)


def _script() -> str:
    return HANDOFF_SCRIPT.read_text(encoding="utf-8")


def test_no_compose_verb_hardcodes_python_service() -> None:
    """Every ``podman compose <verb>`` call site targets the derived variable,
    and the script contains no functional fallback to the coordinator's own
    service name — the exact bug #2147 exists to fix."""
    script = _script()
    for verb in ("stop", "start", "restart", "run --rm --no-deps", "exec -T"):
        assert f"{verb} python-service" not in script, (
            f"'podman compose {verb} python-service' is hardcoded; it must use "
            '"$worker_service" (or "${compose_words[@]}"), derived from '
            "FLEET_WORKER_SERVICE, the same way the desk authors its own "
            "restart command."
        )

    non_comment_lines = [
        line for line in script.splitlines() if not line.strip().startswith("#")
    ]
    functional_references = [
        line for line in non_comment_lines if "python-service" in line
    ]
    assert functional_references == [], (
        f"unexpected non-comment reference(s) to python-service: {functional_references}; "
        "the script must require FLEET_WORKER_SERVICE rather than default to the "
        "coordinator's own service name."
    )


def _run_header(env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
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
    return subprocess.run(
        ["bash", "-c", header + probe],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _resolved(env: dict[str, str], cwd: Path) -> dict[str, str]:
    completed = _run_header(env, cwd)
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


def _shell_env_for(target: WorkerRestartTarget) -> dict[str, str]:
    """The FLEET_* shell exports the Configuration page's copy button would
    splice in for this target (`configuration-handoff-script.component.ts`'s
    `workerExportLines`) — the same four variables the script's header reads
    directly, with no `.env` fallback."""
    env = {"PATH": "/usr/bin:/bin", "FLEET_WORKER_SERVICE": target.service}
    if target.compose_project is not None:
        env["FLEET_COMPOSE_PROJECT"] = target.compose_project
    if target.compose_files:
        env["FLEET_COMPOSE_FILES"] = ",".join(target.compose_files)
    if target.compose_profile is not None:
        env["FLEET_COMPOSE_PROFILE"] = target.compose_profile
    return env


@pytest.mark.parametrize(
    "target",
    [
        WorkerRestartTarget(
            service="python-service", compose_project=None, compose_files=(), compose_profile=None
        ),
        WorkerRestartTarget(
            service="alpaca-paper-clerk",
            compose_project=None,
            compose_files=("compose.yaml", "compose.fleet.dev.yaml"),
            compose_profile=None,
        ),
        WorkerRestartTarget(
            service="alpaca-live-clerk",
            compose_project="learn-ai-fleet",
            compose_files=("compose.yaml", "compose.fleet.yaml"),
            compose_profile="fleet",
        ),
    ],
    ids=["combined-dev", "dev-two-lane", "fleet"],
)
def test_derivation_matches_the_canonical_restart_command(
    target: WorkerRestartTarget, tmp_path: Path
) -> None:
    """Both sides execute: given this lane's exact shell-exported FLEET_*
    variables, the script's header resolves the identical compose invocation
    `worker_restart_command` (desk_state.py) authors for the same target —
    the value the Configuration page's copy button actually fills in."""
    canonical = worker_restart_command(target)
    assert canonical is not None
    expected_suffix = f" restart {target.service}"
    assert canonical.endswith(expected_suffix)
    canonical_words = canonical[: -len(expected_suffix)]

    resolved = _resolved(_shell_env_for(target), _fake_repo(tmp_path))

    assert resolved["WORKER_SERVICE"] == target.service
    assert resolved["COMPOSE_WORDS"] == canonical_words


def test_a_shell_exported_worker_service_is_the_only_source(tmp_path: Path) -> None:
    """The script reads FLEET_WORKER_SERVICE directly; nothing else (a
    repo-root .env, an unset default) can supply it — matching the finding
    that neither .env nor .env.example ever declares a FLEET_* key."""
    resolved = _resolved(
        {"PATH": "/usr/bin:/bin", "FLEET_WORKER_SERVICE": "alpaca-paper-clerk"},
        _fake_repo(tmp_path),
    )

    assert resolved["WORKER_SERVICE"] == "alpaca-paper-clerk"
    assert resolved["COMPOSE_WORDS"] == "podman compose"


def test_derivation_trims_a_spaced_file_list_like_fleet_settings_does(tmp_path: Path) -> None:
    """A FLEET_COMPOSE_FILES value with a space after the comma — legal input,
    since FleetSettings.get_compose_files() (config.py's _split_compose_files)
    strips each entry — resolves to the same two file names on the bash side,
    not a second file name with a leading space baked into the `-f` argument."""
    raw = "compose.yaml, compose.fleet.yaml"
    canonical_files = FleetSettings(COMPOSE_FILES=raw).get_compose_files()
    assert canonical_files == ("compose.yaml", "compose.fleet.yaml")

    resolved = _resolved(
        {
            "PATH": "/usr/bin:/bin",
            "FLEET_WORKER_SERVICE": "alpaca-live-clerk",
            "FLEET_COMPOSE_FILES": raw,
        },
        _fake_repo(tmp_path),
    )

    assert resolved["COMPOSE_WORDS"] == "podman compose " + " ".join(
        f"-f {compose_file}" for compose_file in canonical_files
    )


def test_derivation_refuses_loudly_when_worker_service_is_unset(tmp_path: Path) -> None:
    """No FLEET_WORKER_SERVICE means an immediate, loud refusal — never a
    silent fall-through to whatever the bare command would resolve to. This
    is the regression #2147 exists to close: the script used to default to
    python-service, the coordinator, and restart the wrong container."""
    completed = _run_header({"PATH": "/usr/bin:/bin"}, _fake_repo(tmp_path))

    assert completed.returncode != 0
    assert "FLEET_WORKER_SERVICE" in completed.stderr
    assert completed.stdout == ""


# ---- #2158: the Configuration HTTP surface is posture-aware ---------------


def _script_function(name: str) -> str:
    """Slice one function definition out of the script verbatim, so the
    tests execute the script's own logic instead of a re-typed copy."""
    lines = _script().splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].rstrip("\n") == "}")
    return "".join(lines[start : end + 1])


def _selection_url(fleet_clerk_id: str) -> str:
    """The selection URL the script's own route builder resolves for one
    posture: ``fleet_clerk_id=""`` is the combined posture, a ``clrk_`` id a
    fleet one."""
    probe = (
        'data_plane_url="http://127.0.0.1:8000"\n'
        f'fleet_clerk_id="{fleet_clerk_id}"\n'
        f"{_script_function('config_route')}\n"
        "config_route /configuration/selection\n"
    )
    completed = subprocess.run(
        ["bash", "-c", probe], capture_output=True, text=True, timeout=10
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


@pytest.mark.parametrize(
    ("fleet_clerk_id", "expected_prefix"),
    [
        ("", "/api/brokers/alpaca"),
        ("clrk_0123456789abcdef01234567", "/api/brokers/alpaca/clerks/clrk_0123456789abcdef01234567"),
    ],
    ids=["combined-direct", "fleet-clerk-scoped"],
)
def test_selection_url_matches_the_catalog_route_the_configuration_page_uses(
    fleet_clerk_id: str, expected_prefix: str
) -> None:
    """#2158: on a fleet posture the ceremony reaches the lane's
    Configuration surface through the coordinator's clerk-scoped catalog
    route — the same URL ``operationUrl('configuration_selection_read',
    {broker, clerkId})`` builds for the Configuration page (``clerkScope``
    plus the committed catalog snapshot's ``path_template``) — never the
    in-process clerk route the coordinator does not mount. The combined
    posture keeps its direct route."""
    snapshot = json.loads(
        (
            REPOSITORY_ROOT
            / "Frontend"
            / "src"
            / "app"
            / "fleet"
            / "fleet-operation-catalog.snapshot.json"
        ).read_text(encoding="utf-8")
    )
    template = snapshot["operations"]["configuration_selection_read"]["path_template"]
    assert _selection_url(fleet_clerk_id) == (
        f"http://127.0.0.1:8000{expected_prefix}{template}"
    )


@pytest.mark.parametrize(
    ("coordinator_url", "expected_service"),
    [
        ("http://python-service:8000", "python-service"),
        ("http://fleet-coordinator:8000", "fleet-coordinator"),
    ],
    ids=["dev-two-lane", "production-fleet"],
)
def test_the_control_secret_is_read_from_the_lane_declared_coordinator(
    coordinator_url: str, expected_service: str
) -> None:
    """#2158: a fleet lane holds no DATA_PLANE_CONTROL_SECRET; the
    coordinator does. The script derives the coordinator's compose service
    name from the lane's own FLEET_COORDINATOR_URL declaration — executing
    the script's own derivation lines, not a copy — so it never guesses a
    service name, the exact failure mode FLEET_WORKER_SERVICE's
    required-variable refusal exists to prevent."""
    script = _script()
    lines = script.splitlines(keepends=True)
    start = next(
        i
        for i, line in enumerate(lines)
        if line.lstrip().startswith('coordinator_service="${coordinator_url#*://}"')
    )
    derivation = "".join(lines[start : start + 2])
    probe = (
        f'coordinator_url="{coordinator_url}"\n'
        f"{derivation}"
        'printf "%s" "$coordinator_service"\n'
    )
    completed = subprocess.run(
        ["bash", "-c", probe], capture_output=True, text=True, timeout=10
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == expected_service
    # The secret exec itself targets that derived name, never a hardcoded
    # service, and every lane-env exec tolerates the combined posture's
    # unset variables under `set -euo pipefail`.
    assert (
        '"${compose_words[@]}" exec -T "$coordinator_service" '
        "printenv DATA_PLANE_CONTROL_SECRET" in script
    )
    for variable in ("FLEET_COORDINATOR_URL", "FLEET_CLERK_ID"):
        assert f"printenv {variable} 2>/dev/null || true" in script


def test_the_posture_choice_lives_in_config_route_alone() -> None:
    """No call site outside ``config_route`` builds an /api/brokers URL by
    hand — the posture choice (#2158) exists in exactly one place, so a
    future edit cannot reintroduce a fleet-posture-deaf direct URL."""
    lines = _script().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("config_route() {"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    outside = [
        line
        for i, line in enumerate(lines)
        if not start <= i <= end
        and not line.strip().startswith("#")
        and "/api/brokers" in line
    ]
    assert outside == []


@pytest.mark.parametrize(
    ("published", "expected"),
    [
        ("127.0.0.1:8100", "127.0.0.1:8100"),
        ("0.0.0.0:8100", "127.0.0.1:8100"),
        ("0.0.0.0:8000", "127.0.0.1:8000"),
    ],
    ids=["explicit-loopback", "wildcard-production", "wildcard-dev"],
)
def test_published_host_address_normalizes_a_wildcard_binding(
    published: str, expected: str
) -> None:
    """A `compose port` answer with a wildcard binding still answers on
    loopback; an explicit host is used verbatim — executing the script's
    own normalization function, not a copy of it."""
    probe = f"{_script_function('published_host_address')}\npublished_host_address \"{published}\"\n"
    completed = subprocess.run(
        ["bash", "-c", probe], capture_output=True, text=True, timeout=10
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == expected


def test_the_fleet_path_derives_the_coordinators_published_port() -> None:
    """The production posture publishes the coordinator's in-container 8000
    as 127.0.0.1:${FLEET_COORDINATOR_PORT:-8100} (compose.fleet.yaml), so a
    fleet-posture run must read the published port from the deployment via
    `compose port` rather than reuse the combined posture's
    127.0.0.1:8000 — which targets a closed port and aborts the ceremony
    before Apply."""
    script = _script()
    assert '"${compose_words[@]}" port "$coordinator_service" 8000' in script
    assert 'data_plane_url="http://$(published_host_address "$published_port_line")"' in script


def _directory(*lanes: tuple[str, str]) -> str:
    return json.dumps({"observed_at_ms": 1, "clerks": [
        {"broker": broker, "clerk_id": clerk_id} for broker, clerk_id in lanes
    ]})


def _settings_page(*, fleet_clerk_id: str, directory: str | None) -> subprocess.CompletedProcess[str]:
    """Run the script's own ``settings_page_url`` against a stubbed ``curl``
    that answers the account directory (or fails, when ``directory`` is
    ``None``), exactly as the ceremony calls it after the worker restarts."""
    curl = (
        "curl() { echo 'curl: (7) Failed to connect to 127.0.0.1 port 8000' >&2; return 7; }\n"
        if directory is None
        else f"curl() {{ printf '%s' {json.dumps(directory)}; }}\n"
    )
    probe = (
        'data_plane_url="http://127.0.0.1:8000"\n'
        'control_secret="secret"\n'
        f'fleet_clerk_id="{fleet_clerk_id}"\n'
        f"{curl}"
        f"{_script_function('single_alpaca_lane')}\n"
        f"{_script_function('settings_page_url')}\n"
        "settings_page_url\n"
    )
    return subprocess.run(["bash", "-c", probe], capture_output=True, text=True, timeout=10)


def test_a_fleet_lane_opens_its_own_settings_page() -> None:
    completed = _settings_page(fleet_clerk_id="clrk_paper", directory=None)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "http://localhost:4200/brokers/alpaca/clerks/clrk_paper/settings"


def test_the_combined_posture_opens_the_directorys_one_alpaca_lane() -> None:
    """#2567 (CodeRabbit on #2568): a combined worker names no FLEET_CLERK_ID,
    and the ceremony used to open the broker-wide /brokers/alpaca/settings —
    a page that could never stage or Apply. It now opens the one Alpaca
    lane the same data plane's account directory lists; another broker's
    lanes never count."""
    completed = _settings_page(
        fleet_clerk_id="", directory=_directory(("alpaca", "clrk_combined"), ("ibkr", "clrk_other")),
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "http://localhost:4200/brokers/alpaca/clerks/clrk_combined/settings"


@pytest.mark.parametrize(
    "directory",
    [
        _directory(("alpaca", "clrk_a"), ("alpaca", "clrk_b")),
        _directory(),
        None,
    ],
    ids=["two-alpaca-lanes", "no-alpaca-lane", "directory-unreadable"],
)
def test_the_combined_posture_refuses_rather_than_guess_a_lane(directory: str | None) -> None:
    """With several Alpaca lanes, none, or no readable directory, no single
    lane can be named: ``settings_page_url`` fails (the ceremony then prints
    the by-hand procedure and exits 1) and never prints a URL."""
    completed = _settings_page(fleet_clerk_id="", directory=directory)

    assert completed.returncode != 0
    assert completed.stdout == ""


@pytest.mark.parametrize(
    ("directory", "reason"),
    [
        (None, "The account directory (http://127.0.0.1:8000/api/broker-clerks) could not be read: "
               "curl: (7) Failed to connect to 127.0.0.1 port 8000"),
        (_directory(("alpaca", "clrk_a"), ("alpaca", "clrk_b")), "does not list exactly one Alpaca lane"),
        ("<html>proxy error</html>", "is not the lane list this script reads"),
    ],
    ids=["directory-unreadable", "two-alpaca-lanes", "not-a-lane-list"],
)
def test_the_refusal_says_why_no_lane_was_named(directory: str | None, reason: str) -> None:
    """Review A (minor): an unreachable directory was reported as "does not
    list exactly one Alpaca lane", because curl's own error was discarded.
    Each failure now names itself, with curl's error when it has one."""
    completed = _settings_page(fleet_clerk_id="", directory=directory)

    assert completed.returncode != 0
    assert reason in completed.stderr
