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
from pathlib import Path

import yaml

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
    execution credentials the password the backend itself uses. The
    snapshot render resolves env_file against the committed examples, so
    the key's presence there is the env_file path working. The production
    ``compose.fleet.yaml`` posture is out of scope here: its clerks sit on
    ``fleet-private`` alone, so lake access there is a routing decision,
    not a missing variable."""
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
        assert "POSTGRES_URL" in detail[lane]["environment_keys"], (
            f"{lane} resolves no POSTGRES_URL from its env_file: every "
            "lake-backed lane read 500s (#2163)"
        )
        assert detail[lane]["env_file"], (
            f"{lane} declares no env_file: POSTGRES_URL must reach the lane "
            "that way (#2166)"
        )
