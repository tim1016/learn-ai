"""Contract: the committed fleet topology snapshot pins the deployed shape.

`deploy/fleet/topology.snapshot.json` is produced by
`scripts/render_fleet_topology.py` from `compose.yaml` + `compose.fleet.dev.yaml`.
It is committed so CI can catch a topology drift (a renamed custody volume, a
role split that quietly disappears) as a diff, without ever rendering a real
secret into a tracked file. These tests pin the facts that matter most: the
role split is real, the custody volume names are exactly what Live custody
already answers to, and the snapshot never carries a value — only key names.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import yaml

from tests.contracts.compose_files import render_module

ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT = ROOT / "deploy" / "fleet" / "topology.snapshot.json"


def _snapshot() -> dict:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def test_alpaca_lanes_retain_read_only_ibkr_market_data() -> None:
    """Retiring IBKR order control must never disable Alpaca's data source."""
    topology = yaml.load((ROOT / "compose.fleet.dev.yaml").read_text(), Loader=yaml.BaseLoader)
    environment = topology["x-alpaca-clerk-agent"]["environment"]
    assert environment["IBKR_BROKER_ENABLED"] == "true"
    assert environment["IBKR_READONLY"] == "true"
    services = topology["services"]
    assert services["alpaca-live-clerk"]["environment"]["IBKR_CLIENT_ID"] != (
        services["alpaca-paper-clerk"]["environment"]["IBKR_CLIENT_ID"]
    )


def test_snapshot_pins_the_deployed_role_split() -> None:
    detail = _snapshot()["service_detail"]
    assert detail["python-service"]["environment_keys"].count("FLEET_ROLE") == 1
    for lane in ("alpaca-live-clerk", "alpaca-paper-clerk"):
        assert "FLEET_AGENT_ENDPOINT_REF" in detail[lane]["environment_keys"]


def test_snapshot_pins_the_custody_volume_names() -> None:
    """A renamed custody volume orphans a lane. This is the tripwire."""
    volumes = _snapshot()["volumes"]
    assert volumes["alpaca-clerk-data"] == {"name": "learn-ai-alpaca-clerk-data", "external": True}
    assert volumes["alpaca-paper-clerk-data"]["name"] == "learn-ai-alpaca-paper-clerk-data"


def test_snapshot_carries_no_environment_values() -> None:
    """The snapshot is committed; it must be secret-free by construction."""
    text = SNAPSHOT.read_text(encoding="utf-8")
    assert "environment\":" not in text and '"environment"' not in text
    for service in _snapshot()["service_detail"].values():
        assert isinstance(service["environment_keys"], list)
        assert all(isinstance(key, str) and "=" not in key for key in service["environment_keys"])


def test_snapshot_confirms_the_coordinator_clerk_dir_fence_is_a_checked_fact() -> None:
    """ALPACA_CLERK_DIR="" on the coordinator neutralizes the base clerk path
    so no clerk surface can silently reappear there. This turns that
    invariant into something CI actually verifies, not just a comment — while
    still never persisting the (non-secret, but still not needed) value."""
    fenced = _snapshot()["fenced_empty_env_keys"]
    assert fenced["python-service"]["ALPACA_CLERK_DIR"] is True
    for lane in ("alpaca-live-clerk", "alpaca-paper-clerk"):
        assert fenced[lane]["ALPACA_CLERK_DIR"] is False, (
            f"{lane} must mount a real clerk-state directory, not the coordinator's fence."
        )


def test_snapshot_declares_env_file_wiring_for_every_credential_bearing_service() -> None:
    """The whole point of this work order: env_file must actually govern.
    A service with no env_file declaration at all cannot be credentialed
    through the externalised path."""
    detail = _snapshot()["service_detail"]
    for service in ("python-service", "alpaca-live-clerk", "alpaca-paper-clerk"):
        assert detail[service]["env_file"], f"{service} declares no env_file source"


def test_snapshot_pins_the_clerk_containment_limits() -> None:
    """Both clerks express their cpu/memory limits under `deploy.resources.limits`,
    not the short top-level `cpus:`/`mem_limit` syntax. A projection that only
    reads the top-level fields records `{}` here regardless of what the overlay
    actually sets -- deleting a limit would then produce no diff, despite the
    renderer claiming to pin containment. `mem_limit` is compose's normalised,
    byte-denominated rendering of `memory: 768m`; the exact byte count is not
    the point, only that some non-empty value survives the projection."""
    containment = _snapshot()["service_detail"]
    for lane in ("alpaca-live-clerk", "alpaca-paper-clerk"):
        assert containment[lane]["containment"], f"{lane} recorded no containment limits at all"
        assert "cpus" in containment[lane]["containment"]
        assert "mem_limit" in containment[lane]["containment"]


def test_snapshot_pins_lake_catalog_access_for_every_clerk_lane() -> None:
    """#2163: a lane-served read (bot panel, strategy-validation golden
    dossiers) resolves lake evidence over asyncpg in the lane process itself,
    so a clerk without ``POSTGRES_URL`` 500s on every lake-backed read --
    which the coordinator then masked as a lane-identity 409 (#2164).

    #2166: the key must arrive via ``env_file`` only, naming the read-only
    ``fleet_lake_catalog`` role. The overlay's own environment anchor must
    not declare it at all -- ``environment:`` outranks ``env_file:``
    key-for-key, so even a ${}-templated superuser URL there would silently
    defeat the lane env files and hand a lane that also carries Alpaca
    execution credentials the password the backend itself uses. The env
    example files deliberately carry the documented line COMMENTED (the
    production rollout copies them verbatim and its clerks cannot reach
    ``db``), so the merged snapshot render resolves no value for the key --
    the presence pin lives in the env-completeness contract instead. The
    production ``compose.fleet.yaml`` posture is out of scope here: its
    clerks sit on ``fleet-private`` alone, so lake access there is a
    routing decision, not a missing variable."""
    overlay = yaml.load(
        (ROOT / "compose.fleet.dev.yaml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    assert "POSTGRES_URL" not in overlay["x-alpaca-clerk-agent"]["environment"], (
        "the clerk anchor declares POSTGRES_URL inline: `environment:` outranks "
        "env_file key-for-key, so the lane env files' least-privilege "
        "fleet_lake_catalog URL (#2166) would be silently defeated"
    )
    detail = _snapshot()["service_detail"]
    for lane in ("alpaca-live-clerk", "alpaca-paper-clerk"):
        assert "POSTGRES_URL" not in detail[lane]["environment_keys"], (
            f"{lane} resolves a POSTGRES_URL the commented examples do not "
            "carry: an inline declaration has re-entered the topology (#2166)"
        )
        assert detail[lane]["env_file"], (
            f"{lane} declares no env_file: POSTGRES_URL must reach the lane "
            "that way (#2166)"
        )


_ENV_FILE_INDIRECTION = re.compile(r"^\$\{(?P<variable>[A-Z][A-Z0-9_]*):-(?P<default>[^}]+)\}$")


def test_every_rendered_env_file_path_redirects_to_a_committed_example() -> None:
    """#2235: a literal `env_file:` path makes the snapshot machine-dependent.

    `compose config` merges every key an `env_file:` declares into the
    rendered `environment:`, and the render projects those key *names* into
    the snapshot. A path written as a literal therefore records whatever the
    rendering machine's gitignored file happens to declare: `compose.yaml`
    named `./PythonDataService/.env` directly, so a developer who had one
    absorbed its keys while CI (which has no such file) did not. The snapshot
    only that machine could reproduce then read as "master's snapshot is
    stale" on an untouched tree.

    The fix is the indirection the three fleet env files already use, and this
    is the tripwire for the next one: every path the render reads must be a
    `${VAR:-default}`, `VAR` must have a default in the render script's
    `ENV_FILE_DEFAULTS`, and that default must name a file the repo actually
    commits. It asserts against `raw_env_file_paths()` — the reader whose
    output the snapshot records — rather than walking the YAML again, so the
    contract cannot drift away from the thing it is pinning, and a new overlay
    in `COMPOSE_FILES` is covered automatically.

    What this deliberately does not promise: an operator who exports `VAR`
    themselves still renders from their own file. `setdefault` is the existing,
    documented behaviour for the fleet paths and is left alone — the bug was a
    path with no indirection to override, not an override that works.
    """
    render = render_module()
    defaults: dict[str, str] = render.ENV_FILE_DEFAULTS
    tracked = set(
        subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.split("\0")
    )
    declared = render.raw_env_file_paths()
    assert declared, (
        "the render read no env_file paths at all — this contract would pass "
        "having checked nothing (#2235)"
    )

    for service, paths in declared.items():
        for path in paths:
            match = _ENV_FILE_INDIRECTION.match(str(path))
            assert match, (
                f"service {service!r} declares env_file {path!r} as a literal path. "
                "Write it as ${VAR:-default} and give VAR a committed-example default "
                "in render_fleet_topology.ENV_FILE_DEFAULTS, or the render absorbs "
                "whatever that gitignored file declares on the rendering machine (#2235)."
            )
            variable = match.group("variable")
            assert variable in defaults, (
                f"service {service!r} indirects env_file through ${{{variable}}}, but "
                "render_fleet_topology.ENV_FILE_DEFAULTS has no default for it, so the "
                f"render still resolves {match.group('default')!r} (#2235)."
            )
            example = defaults[variable].removeprefix("./")
            assert example in tracked, (
                f"ENV_FILE_DEFAULTS[{variable!r}] points at {defaults[variable]!r}, which "
                "this repo does not commit — the render would be reproducible only where "
                "that file happens to exist (#2235)."
            )
