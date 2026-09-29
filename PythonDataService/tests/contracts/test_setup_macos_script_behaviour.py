"""Behavioural guard on `setup-macos.sh`, the macOS first-run bootstrap (#2575).

Two questions matter, and a text-matching test cannot answer either:

- *What does the script touch on a machine that already runs the fleet
  posture?* Re-running it stops the Podman VM and brings the stack back from
  `compose.yaml` alone -- the data plane on the Live clerk's volume, in the
  combined posture, while the clerks are gone. On such a machine it must
  refuse before it contacts brew or podman at all.
- *What does a fresh machine get?* The bind-mount host directories, the Clerk
  volume marker and the data-lake root identity are the three steps the data
  plane refuses to start without (exit 78 and exit 3). Doing them, and doing
  them only when it is provably a first install, is what stops a first run
  timing out on its own health wait.

So this drives the real script, copied into a scratch checkout under a scratch
``$HOME``, against fake ``podman``/``brew``/``node`` tools that record their
calls. ``PATH`` is the fake directory plus ``/usr/bin:/bin`` only, so a real
``podman`` (Homebrew installs it elsewhere) can never be reached. The fake
podman models the Clerk volume as a real directory and runs the volume-check
payload the script hands ``compose run`` against it, so the "first install
only" guard is exercised, not assumed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SETUP_SCRIPT = REPOSITORY_ROOT / "setup-macos.sh"
FLEET_OVERLAY = REPOSITORY_ROOT / "compose.fleet.dev.yaml"

_BASH = "/bin/bash" if Path("/bin/bash").exists() else (shutil.which("bash") or "bash")
_CLERK_VOLUME = "learn-ai-alpaca-clerk-data"
_GENERATED_UUID = "2F0C5A4E-1B7D-4C3A-9E58-6D4B7A1C9F30"
_ENV_UUID = "77a98bfb-e395-4dfc-ab52-3c0928fa20a2"

# One executable, symlinked once per tool it impersonates: the tool it plays is
# read from the name it was invoked by.
_FAKE_TOOL = r'''#!__PYTHON__
import json, os, subprocess, sys
from pathlib import Path

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
state = Path(os.environ["FAKE_STATE"])
with (state / "calls.jsonl").open("a") as handle:
    handle.write(json.dumps([tool, *args]) + "\n")

if tool == "uname":
    print("Darwin")
elif tool == "sysctl":
    print({"hw.ncpu": "8", "hw.memsize": str(16 * 1024**3)}[args[-1]])
elif tool == "uuidgen":
    print(os.environ["FAKE_UUID"])
elif tool == "podman":
    volumes = state / "volumes"
    if args[:2] == ["machine", "list"]:
        print("podman-machine-default*")
    elif args[:2] == ["machine", "inspect"]:
        print("running")
    elif args[:2] == ["volume", "exists"]:
        sys.exit(0 if (volumes / args[2]).is_dir() else 1)
    elif args[:2] == ["volume", "create"]:
        (volumes / args[2]).mkdir(parents=True)
        print(args[2])
    elif args[:2] == ["compose", "run"] and "--entrypoint" in args:
        volume = volumes / "learn-ai-alpaca-clerk-data"
        if not volume.is_dir():
            print("volume learn-ai-alpaca-clerk-data declared as external, but could not be found", file=sys.stderr)
            sys.exit(1)
        payload = args[args.index("-c") + 1]
        payload = payload.replace("/app/artifacts/alpaca_clerk", str(volume))
        payload = payload.replace("/app/alpaca_clerk_legacy", os.environ["FAKE_LEGACY_DIR"])
        sys.exit(subprocess.run(["/bin/sh", "-c", payload]).returncode)
    elif args[:2] == ["compose", "run"] and "scripts.manage_data_root" in args:
        marker = Path(os.environ["FAKE_LAKE_ROOT"]) / "lake" / ".data-root.json"
        if marker.exists():
            print(f"{marker} already exists; refusing to re-initialize it", file=sys.stderr)
            sys.exit(1)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"schema_version": 1, "data_root_id": args[args.index("--root-id") + 1]}))
    elif args[:2] == ["compose", "ps"]:
        print("fake compose ps")
sys.exit(0)
'''

_FAKE_TOOLS = ("podman", "brew", "node", "curl", "sleep", "uname", "sysctl", "uuidgen")


@dataclass
class Machine:
    home: Path
    repo: Path
    state: Path
    env: dict[str, str]

    def run(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [_BASH, str(self.repo / "setup-macos.sh"), *argv],
            capture_output=True, text=True, env=self.env, cwd=self.repo, timeout=60,
        )

    def calls(self) -> list[list[str]]:
        log = self.state / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def podman_calls(self) -> list[list[str]]:
        return [call[1:] for call in self.calls() if call[0] == "podman"]

    def touched_the_machine(self) -> list[list[str]]:
        return [call for call in self.calls() if call[0] in {"brew", "podman", "node"}]

    @property
    def lake_marker(self) -> Path:
        return Path(self.env["FAKE_LAKE_ROOT"]) / "lake" / ".data-root.json"

    @property
    def clerk_volume(self) -> Path:
        return self.state / "volumes" / _CLERK_VOLUME

    @property
    def python_env_file(self) -> Path:
        return self.repo / "PythonDataService" / ".env"


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    assert shutil.which("podman", path="/usr/bin:/bin") is None, (
        "a real podman is reachable on the isolated PATH; refusing to run setup-macos.sh"
    )
    assert shutil.which("brew", path="/usr/bin:/bin") is None

    home = tmp_path / "home"
    repo = home / "learn-ai"
    state = tmp_path / "state"
    bin_dir = tmp_path / "bin"
    for directory in (repo / "PythonDataService", repo / "Frontend/scripts", repo / "Frontend/src/environments",
                      state, bin_dir):
        directory.mkdir(parents=True)

    shutil.copy(SETUP_SCRIPT, repo / "setup-macos.sh")
    shutil.copy(FLEET_OVERLAY, repo / "compose.fleet.dev.yaml")
    (repo / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (repo / ".env.example").write_text(
        "POLYGON_API_KEY=your_polygon_api_key_here\nDATA_PLANE_CONTROL_SECRET=\n", encoding="utf-8"
    )
    (repo / "PythonDataService/.env.example").write_text(
        "POLYGON_API_KEY=your_polygon_api_key_here\n", encoding="utf-8"
    )
    (repo / "Frontend/scripts/data-plane-control-secret.cjs").write_text("", encoding="utf-8")
    (repo / "Frontend/src/environments/environment.development.ts.example").write_text(
        "export const environment = {};\n", encoding="utf-8"
    )

    fake = bin_dir / "fake_tool"
    fake.write_text(_FAKE_TOOL.replace("__PYTHON__", sys.executable), encoding="utf-8")
    fake.chmod(0o755)
    for tool in _FAKE_TOOLS:
        (bin_dir / tool).symlink_to(fake)

    return Machine(
        home=home, repo=repo, state=state,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(home),
            "FAKE_STATE": str(state),
            "FAKE_UUID": _GENERATED_UUID,
            "FAKE_LAKE_ROOT": str(repo / "data-lake-volume"),
            "FAKE_LEGACY_DIR": str(repo / "PythonDataService/artifacts/alpaca_clerk"),
        },
    )


def _add_lane_env_file(machine: Machine, name: str) -> Path:
    lane_file = machine.repo / "deploy/fleet/env" / name
    lane_file.parent.mkdir(parents=True, exist_ok=True)
    lane_file.write_text("FLEET_CLERK_ID=clrk_example\n", encoding="utf-8")
    return lane_file


@pytest.mark.parametrize("lane_file_name", ["live.env", "paper.env"])
def test_a_fleet_machine_is_refused_before_anything_is_touched(machine: Machine, lane_file_name: str) -> None:
    _add_lane_env_file(machine, lane_file_name)

    completed = machine.run()

    assert completed.returncode != 0
    assert "./restart.sh" in completed.stderr, completed.stderr
    assert machine.touched_the_machine() == [], (
        "re-running setup-macos.sh on a fleet machine must not reach brew or podman: "
        f"{machine.touched_the_machine()}"
    )
    assert not (machine.repo / ".env").exists(), "a refused run must not write env files"


def test_a_lane_env_file_at_an_overridden_path_also_marks_a_fleet_machine(machine: Machine) -> None:
    elsewhere = machine.home / "secrets" / "live-lane.env"
    elsewhere.parent.mkdir()
    elsewhere.write_text("FLEET_CLERK_ID=clrk_example\n", encoding="utf-8")
    machine.env["FLEET_LIVE_ENV_FILE"] = str(elsewhere)

    completed = machine.run()

    assert completed.returncode != 0
    assert machine.touched_the_machine() == []


def test_the_host_ng_serve_flag_is_gone(machine: Machine) -> None:
    completed = machine.run("--serve")

    assert completed.returncode == 2
    assert "unknown argument: --serve" in completed.stderr
    assert machine.touched_the_machine() == []


def test_help_documents_no_serve_flag(machine: Machine) -> None:
    completed = machine.run("--help")

    assert completed.returncode == 0
    assert "Usage:" in completed.stdout
    assert "--serve" not in completed.stdout
    assert machine.touched_the_machine() == []


def test_a_fresh_run_prepares_the_host_and_both_ceremonies_before_the_stack_starts(machine: Machine) -> None:
    completed = machine.run()

    assert completed.returncode == 0, completed.stdout + completed.stderr
    for directory in (
        machine.home / "Lean/Data",
        machine.repo / "data-lake-volume",
        machine.repo / "PythonDataService/cache",
        machine.repo / "PythonDataService/lean-cache",
        machine.repo / "PythonDataService/artifacts/alpaca_clerk",
    ):
        assert directory.is_dir(), f"bind-mount source {directory} was not created"
    assert (machine.repo / "Frontend/src/environments/environment.development.ts").is_file()

    assert (machine.clerk_volume / "_compose_volume_ready").is_file(), "the Clerk volume was not marked ready"
    assert not (machine.clerk_volume / "_compose_volume_ready").is_symlink()
    lake_id = json.loads(machine.lake_marker.read_text(encoding="utf-8"))["data_root_id"]
    assert lake_id == _GENERATED_UUID.lower()
    assert f"DATA_LAKE_ROOT_ID={lake_id}\n" in machine.python_env_file.read_text(encoding="utf-8")

    verbs = [" ".join(call[:2]) for call in machine.podman_calls()]
    start = verbs.index("compose up")
    assert verbs.index("volume create") < start
    assert [i for i, verb in enumerate(verbs) if verb == "compose run"][-1] < start, (
        "both ceremonies must finish before the stack is started"
    )
    assert "ng serve" not in completed.stdout


def test_a_rerun_does_not_repeat_the_ceremonies(machine: Machine) -> None:
    assert machine.run().returncode == 0
    marker_before = machine.lake_marker.read_text(encoding="utf-8")
    calls_before = len(machine.podman_calls())

    completed = machine.run()

    assert completed.returncode == 0, completed.stdout + completed.stderr
    rerun = [" ".join(call[:2]) for call in machine.podman_calls()[calls_before:]]
    assert "volume create" not in rerun
    assert machine.lake_marker.read_text(encoding="utf-8") == marker_before
    assert machine.python_env_file.read_text(encoding="utf-8").count("DATA_LAKE_ROOT_ID=") == 1
    assert not any("scripts.manage_data_root" in call for call in machine.podman_calls()[calls_before:])


@pytest.mark.parametrize("where", ["volume", "legacy tree"])
def test_existing_clerk_data_stops_the_run_and_is_never_marked_ready(machine: Machine, where: str) -> None:
    existing_authority = (
        machine.clerk_volume if where == "volume" else machine.repo / "PythonDataService/artifacts/alpaca_clerk"
    )
    existing_authority.mkdir(parents=True)
    (existing_authority / "clerk.db").write_text("", encoding="utf-8")
    if where == "legacy tree":
        (machine.state / "volumes" / _CLERK_VOLUME).mkdir(parents=True)

    completed = machine.run()

    assert completed.returncode != 0
    assert "alpaca-sqlite-clerk-recovery-and-cutover.md" in completed.stderr, completed.stderr
    assert not (machine.clerk_volume / "_compose_volume_ready").exists()
    assert not any(call[:2] == ["compose", "up"] for call in machine.podman_calls()), (
        "the stack must not be started over an unadopted authority tree"
    )
    assert not machine.lake_marker.exists()


def test_a_legacy_tree_that_cannot_be_inspected_is_not_treated_as_empty(machine: Machine) -> None:
    machine.env["FAKE_LEGACY_DIR"] = str(machine.repo / "not-mounted")

    completed = machine.run()

    assert completed.returncode != 0
    assert "could not check the Alpaca Clerk volume" in completed.stderr, completed.stderr
    assert not (machine.clerk_volume / "_compose_volume_ready").exists()
    assert not any(call[:2] == ["compose", "up"] for call in machine.podman_calls())


def test_a_lake_id_already_in_the_env_file_is_the_one_claimed(machine: Machine) -> None:
    machine.python_env_file.write_text(
        f"POLYGON_API_KEY=real\nDATA_LAKE_ROOT_ID={_ENV_UUID}\n", encoding="utf-8"
    )

    completed = machine.run()

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(machine.lake_marker.read_text(encoding="utf-8"))["data_root_id"] == _ENV_UUID
    assert machine.python_env_file.read_text(encoding="utf-8").count("DATA_LAKE_ROOT_ID=") == 1


def test_an_overridden_lake_directory_in_the_env_file_is_the_one_created(machine: Machine) -> None:
    (machine.repo / ".env").write_text(
        "POLYGON_API_KEY=real\nLEAN_DATA_VOLUME_HOST_PATH=./lake-elsewhere\n", encoding="utf-8"
    )
    machine.env["FAKE_LAKE_ROOT"] = str(machine.repo / "lake-elsewhere")

    completed = machine.run()

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (machine.repo / "lake-elsewhere").is_dir()
    assert not (machine.repo / "data-lake-volume").exists()
    assert machine.lake_marker.is_file()
