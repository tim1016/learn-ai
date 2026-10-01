"""Behavioural guard on `setup-macos.sh`, the macOS first-run bootstrap (#2575).

Two questions matter, and a text-matching test cannot answer either:

- *What does the script touch on a machine that already runs the fleet
  posture?* Re-running it stops the Podman VM and brings the stack back from
  `compose.yaml` alone -- the data plane on the Live clerk's volume, in the
  combined posture, while the clerks are gone. On such a machine it must
  refuse before it contacts brew or podman at all.
- *What does a fresh machine get, and what does it refuse to adopt?* The
  bind-mount host directories, the Clerk volume marker and the data-lake root
  identity are the three steps the data plane refuses to start without (exit 78
  and exit 3). The script does them only for a provably first install: it never
  marks a volume that holds an authority tree, and never claims a lake root
  under an identity that belongs to another root.

So this drives the real script, copied into a scratch checkout under a scratch
``$HOME``, against fake ``podman``/``brew``/``node`` tools that record their
calls. The fake directory is first on ``PATH``, so no real tool of those names
is ever reached (the fixture asserts it). The fake podman models what the
script's guards depend on: named volumes are real directories, the payload the
script hands a container runs for real against the mounted paths, podman's
copy-up of image content into a new volume is modelled, and
``scripts.manage_data_root`` is the real command run in-process, so
``inspect`` output and ``init``'s refusals are the real ones.
"""

from __future__ import annotations

import json
import re
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
# Made-up identities: none of these belongs to a real machine.
_GENERATED_UUID = "2F0C5A4E-1B7D-4C3A-9E58-6D4B7A1C9F30"
_ROOT_ID = "0d3f6c1e-8a52-4b7e-9c41-5e2a7b9d1f63"
_OTHER_ROOT_ID = "6b1e94a2-3c07-4d58-a1f9-c2d80e7b5a34"

# Every call is recorded as one record: fields end in 0x1f, the record in 0x1e.
# The trivial tools are shell scripts (one executable, symlinked once per tool,
# named by how it was invoked); only podman needs Python, which keeps a run cheap.
_FAKE_SHELL_TOOL = '''#!/bin/sh
tool=${0##*/}
{ printf '%s\\037' "$tool" "$@"; printf '\\036'; } >> "$FAKE_STATE/calls.log"
case "$tool" in
  uname) echo Darwin ;;
  sysctl) case "$2" in hw.ncpu) echo 8 ;; hw.memsize) echo 17179869184 ;; esac ;;
  uuidgen) echo "$FAKE_UUID" ;;
esac
exit 0
'''

_FAKE_PODMAN = r'''#!__PYTHON__
import os, re, subprocess, sys
from pathlib import Path

args = sys.argv[1:]
state = Path(os.environ["FAKE_STATE"])
with (state / "calls.log").open("a") as handle:
    handle.write("\x1f".join(["podman", *args]) + "\x1f\x1e")

# Podman copies an image's content into a NEW named volume the first time it is
# mounted at a path the image populates. python-service's image creates
# directories under /app/artifacts/alpaca_clerk; a bare image populates nothing.
IMAGE_POPULATED = {"/app/artifacts/alpaca_clerk": "qualification"}


def volume_dir(name):
    volume = state / "volumes" / name
    volume.mkdir(parents=True, exist_ok=True)
    return volume


def mount_volume_at(name, destination):
    volume = volume_dir(name)
    populated = IMAGE_POPULATED.get(destination)
    if populated and not any(volume.iterdir()):
        (volume / populated).mkdir()
    return volume


def run_in_container(payload, mounts):
    # The payload runs for real, with each container path swapped for its host
    # directory. A mount listed in FAKE_SKIP_MOUNTS is left out, as if absent.
    skipped = set(filter(None, os.environ.get("FAKE_SKIP_MOUNTS", "").split(",")))
    for destination, host in mounts.items():
        if destination not in skipped:
            payload = re.sub(r"(?<![\w.])" + re.escape(destination) + r"(?!\w)", str(host), payload)
    return subprocess.run(["/bin/sh", "-c", payload]).returncode


def run_manage_data_root(sub_args):
    # The real administrative command, run in-process from a directory with no
    # .env of its own; --base-root stands in for the container's write root.
    sys.path.insert(0, os.environ["FAKE_PYTHON_SERVICE_DIR"])
    os.environ.setdefault("POLYGON_API_KEY", "fake-key-for-settings")
    os.chdir(state)
    from scripts.manage_data_root import main
    return main([*sub_args, "--base-root", os.environ["FAKE_LAKE_ROOT"]])


if args[:2] == ["machine", "list"]:
    print("podman-machine-default*")
elif args[:2] == ["machine", "inspect"]:
    print("running")
elif args[:2] == ["volume", "exists"]:
    sys.exit(0 if (state / "volumes" / args[2]).is_dir() else 1)
elif args[:2] == ["volume", "create"]:
    (state / "volumes" / args[2]).mkdir(parents=True)
    print(args[2])
elif args[:1] == ["run"]:
    mounts = {}
    for index, arg in enumerate(args):
        if arg == "-v":
            source, destination = args[index + 1].split(":")[:2]
            mounts[destination] = Path(source) if source.startswith("/") else mount_volume_at(source, destination)
    sys.exit(run_in_container(args[args.index("-c") + 1], mounts))
elif args[:2] == ["compose", "run"] and "--entrypoint" in args:
    # python-service's own mounts: the Clerk volume and the read-only legacy tree.
    mounts = {
        "/app/artifacts/alpaca_clerk": mount_volume_at("learn-ai-alpaca-clerk-data", "/app/artifacts/alpaca_clerk"),
        "/app/alpaca_clerk_legacy": Path(os.environ["FAKE_LEGACY_DIR"]),
    }
    sys.exit(run_in_container(args[args.index("-c") + 1], mounts))
elif args[:2] == ["compose", "run"] and "scripts.manage_data_root" in args:
    mount_volume_at("learn-ai-alpaca-clerk-data", "/app/artifacts/alpaca_clerk")
    sys.exit(run_manage_data_root(args[args.index("scripts.manage_data_root") + 1:]))
elif args[:2] == ["compose", "ps"]:
    print("fake compose ps")
sys.exit(0)
'''

_FAKE_SHELL_TOOLS = ("brew", "node", "curl", "sleep", "uname", "sysctl", "uuidgen")
_FAKE_TOOLS = ("podman", *_FAKE_SHELL_TOOLS)


@dataclass
class Machine:
    home: Path
    repo: Path
    state: Path
    env: dict[str, str]

    def run(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [_BASH, str(self.repo / "setup-macos.sh"), *argv],
            capture_output=True, text=True, env=self.env, cwd=self.repo, timeout=120,
        )

    def calls(self) -> list[list[str]]:
        log = self.state / "calls.log"
        if not log.exists():
            return []
        records = log.read_text(encoding="utf-8").split("\x1e")
        return [record.split("\x1f")[:-1] for record in records if record]

    def podman_calls(self) -> list[list[str]]:
        return [call[1:] for call in self.calls() if call[0] == "podman"]

    def touched_the_machine(self) -> list[list[str]]:
        return [call for call in self.calls() if call[0] in {"brew", "podman", "node"}]

    def started_the_stack(self) -> bool:
        return any(call[:2] == ["compose", "up"] for call in self.podman_calls())

    def lake_claims(self) -> int:
        return sum(1 for call in self.podman_calls() if "scripts.manage_data_root" in call and "init" in call)

    def claimed_the_lake(self) -> bool:
        return self.lake_claims() > 0

    @property
    def lake_root(self) -> Path:
        return Path(self.env["FAKE_LAKE_ROOT"])

    @property
    def lake_marker(self) -> Path:
        return self.lake_root / "lake" / ".data-root.json"

    def stamp_lake_root(self, root_id: str) -> None:
        self.lake_marker.parent.mkdir(parents=True, exist_ok=True)
        self.lake_marker.write_text(json.dumps({"schema_version": 1, "data_root_id": root_id}), encoding="utf-8")

    @property
    def clerk_volume(self) -> Path:
        return self.state / "volumes" / _CLERK_VOLUME

    @property
    def python_env_file(self) -> Path:
        return self.repo / "PythonDataService" / ".env"


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    home = tmp_path / "home"
    repo = home / "learn-ai"
    state = tmp_path / "state"
    bin_dir = tmp_path / "bin"
    for directory in (repo / "PythonDataService", repo / "Frontend/scripts", repo / "Frontend/src/environments",
                      state, bin_dir):
        directory.mkdir(parents=True)

    shutil.copy(SETUP_SCRIPT, repo / "setup-macos.sh")
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

    fake_podman = bin_dir / "podman"
    fake_podman.write_text(_FAKE_PODMAN.replace("__PYTHON__", sys.executable), encoding="utf-8")
    fake_podman.chmod(0o755)
    fake_shell_tool = bin_dir / "fake_shell_tool"
    fake_shell_tool.write_text(_FAKE_SHELL_TOOL, encoding="utf-8")
    fake_shell_tool.chmod(0o755)
    for tool in _FAKE_SHELL_TOOLS:
        (bin_dir / tool).symlink_to(fake_shell_tool)

    environment = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home),
        "FAKE_STATE": str(state),
        "FAKE_UUID": _GENERATED_UUID,
        "FAKE_LAKE_ROOT": str(repo / "data-lake-volume"),
        "FAKE_PYTHON_SERVICE_DIR": str(REPOSITORY_ROOT / "PythonDataService"),
        "FAKE_LEGACY_DIR": str(repo / "PythonDataService/artifacts/alpaca_clerk"),
    }
    # The safety property, on whatever this host has installed (a CI runner may
    # ship a real podman in /usr/bin): the fake wins every lookup the script makes.
    for tool in _FAKE_TOOLS:
        assert shutil.which(tool, path=environment["PATH"]) == str(bin_dir / tool), (
            f"a real {tool} would shadow the fake; refusing to run setup-macos.sh"
        )
    return Machine(home=home, repo=repo, state=state, env=environment)


def _overlay_default(variable: str) -> str:
    """The lane env file path `compose.fleet.dev.yaml` interpolates by default."""
    match = re.search(r"\$\{" + variable + r":-([^}]+)\}", FLEET_OVERLAY.read_text(encoding="utf-8"))
    assert match, f"{variable} has no default in {FLEET_OVERLAY.name}"
    return match.group(1).removeprefix("./")


def _add_lane_env_file(machine: Machine, variable: str) -> Path:
    lane_file = machine.repo / _overlay_default(variable)
    lane_file.parent.mkdir(parents=True, exist_ok=True)
    lane_file.write_text("FLEET_CLERK_ID=clrk_example\n", encoding="utf-8")
    return lane_file


@pytest.mark.parametrize("variable", ["FLEET_LIVE_ENV_FILE", "FLEET_PAPER_ENV_FILE"])
def test_a_fleet_machine_is_refused_before_anything_is_touched(machine: Machine, variable: str) -> None:
    # The lane file goes where compose.fleet.dev.yaml says it does, so this
    # fails if the script's defaults ever drift from the overlay's.
    _add_lane_env_file(machine, variable)

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

    # A brand-new volume is checked where the image populates nothing, so the
    # copy-up of the image's own directories cannot make it look populated.
    assert (machine.clerk_volume / "_compose_volume_ready").is_file(), "the Clerk volume was not marked ready"
    assert not (machine.clerk_volume / "_compose_volume_ready").is_symlink()
    lake_id = json.loads(machine.lake_marker.read_text(encoding="utf-8"))["data_root_id"]
    assert lake_id == _GENERATED_UUID.lower()
    assert f"DATA_LAKE_ROOT_ID={lake_id}\n" in machine.python_env_file.read_text(encoding="utf-8")

    verbs = ["run" if call[0] == "run" else " ".join(call[:2]) for call in machine.podman_calls()]
    start = verbs.index("compose up")
    assert verbs.index("volume create") < verbs.index("run") < start
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
    assert machine.lake_claims() == 1, "the lake root is claimed once, on the first run only"


@pytest.mark.parametrize("where", ["volume", "legacy tree"])
def test_existing_clerk_data_stops_the_run_and_is_never_marked_ready(machine: Machine, where: str) -> None:
    existing_authority = (
        machine.clerk_volume if where == "volume" else machine.repo / "PythonDataService/artifacts/alpaca_clerk"
    )
    existing_authority.mkdir(parents=True)
    (existing_authority / "clerk.db").write_text("", encoding="utf-8")
    if where == "legacy tree":
        machine.clerk_volume.mkdir(parents=True)

    completed = machine.run()

    assert completed.returncode != 0
    assert "alpaca-sqlite-clerk-recovery-and-cutover.md" in completed.stderr, completed.stderr
    assert not (machine.clerk_volume / "_compose_volume_ready").exists()
    assert not machine.started_the_stack(), "the stack must not be started over an unadopted authority tree"
    assert not machine.lake_marker.exists()


def test_a_legacy_tree_that_cannot_be_inspected_is_not_treated_as_empty(machine: Machine) -> None:
    machine.env["FAKE_SKIP_MOUNTS"] = "/legacy"

    completed = machine.run()

    assert completed.returncode != 0
    assert "could not check the Alpaca Clerk volume" in completed.stderr, completed.stderr
    assert not (machine.clerk_volume / "_compose_volume_ready").exists()
    assert not machine.started_the_stack()


def test_a_first_install_claims_the_lake_root_under_a_new_id_and_records_it(machine: Machine) -> None:
    completed = machine.run()

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert machine.claimed_the_lake()
    assert machine.python_env_file.read_text(encoding="utf-8").count("DATA_LAKE_ROOT_ID=") == 1


def test_an_id_in_the_env_file_with_no_marker_is_never_claimed_automatically(machine: Machine) -> None:
    """A re-cloned or emptied lake directory is not the root that id named."""
    machine.python_env_file.write_text(
        f"POLYGON_API_KEY=real\nDATA_LAKE_ROOT_ID={_ROOT_ID}\n", encoding="utf-8"
    )
    env_before = machine.python_env_file.read_text(encoding="utf-8")

    completed = machine.run()

    assert completed.returncode != 0
    assert _ROOT_ID in completed.stderr
    assert "manage_data_root init --root-id" in completed.stderr, completed.stderr
    assert not machine.claimed_the_lake()
    assert not machine.lake_marker.exists()
    assert machine.python_env_file.read_text(encoding="utf-8") == env_before
    assert not machine.started_the_stack()


def test_a_marker_with_no_id_in_the_env_file_stops_and_shows_how_to_look(machine: Machine) -> None:
    """Also what a Ctrl-C between the claim and the env write leaves behind."""
    machine.stamp_lake_root(_ROOT_ID)

    completed = machine.run()

    assert completed.returncode != 0
    assert _ROOT_ID in completed.stderr
    assert "scripts.manage_data_root" in completed.stderr and "inspect" in completed.stderr
    assert "DATA_LAKE_ROOT_ID" not in machine.python_env_file.read_text(encoding="utf-8")
    assert not machine.claimed_the_lake()
    assert not machine.started_the_stack()


def test_a_legacy_all_zero_marker_with_no_id_in_the_env_file_is_left_alone(machine: Machine) -> None:
    """An unset DATA_LAKE_ROOT_ID is the legacy all-zero root (app/config.py), so
    a machine stamped before #1876 re-runs cleanly."""
    legacy_root_id = "00000000-0000-0000-0000-000000000000"
    machine.stamp_lake_root(legacy_root_id)

    completed = machine.run()

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not machine.claimed_the_lake()
    assert "DATA_LAKE_ROOT_ID" not in machine.python_env_file.read_text(encoding="utf-8")


def test_a_marker_that_differs_from_the_env_file_id_stops(machine: Machine) -> None:
    machine.stamp_lake_root(_ROOT_ID)
    machine.python_env_file.write_text(
        f"POLYGON_API_KEY=real\nDATA_LAKE_ROOT_ID={_OTHER_ROOT_ID}\n", encoding="utf-8"
    )

    completed = machine.run()

    assert completed.returncode != 0
    assert _ROOT_ID in completed.stderr and _OTHER_ROOT_ID in completed.stderr
    assert not machine.claimed_the_lake()
    assert not machine.started_the_stack()


def test_a_marker_that_matches_the_env_file_id_is_left_alone(machine: Machine) -> None:
    machine.stamp_lake_root(_ROOT_ID)
    machine.python_env_file.write_text(
        f"POLYGON_API_KEY=real\nDATA_LAKE_ROOT_ID={_ROOT_ID.upper()}\n", encoding="utf-8"
    )

    completed = machine.run()

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not machine.claimed_the_lake()
    assert json.loads(machine.lake_marker.read_text(encoding="utf-8"))["data_root_id"] == _ROOT_ID


def test_a_populated_lake_root_is_not_claimed_and_no_id_is_recorded(machine: Machine) -> None:
    populated = machine.lake_root / "lake" / "raw"
    populated.mkdir(parents=True)
    (populated / "artifact.zip").write_bytes(b"x")

    completed = machine.run()

    assert completed.returncode != 0
    assert "could not claim the data-lake root" in completed.stderr, completed.stderr
    assert not machine.lake_marker.exists()
    assert "DATA_LAKE_ROOT_ID" not in machine.python_env_file.read_text(encoding="utf-8")
    assert not machine.started_the_stack()


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
