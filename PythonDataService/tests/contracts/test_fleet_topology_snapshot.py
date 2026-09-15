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

ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT = ROOT / "deploy" / "fleet" / "topology.snapshot.json"


def _snapshot() -> dict:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


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
