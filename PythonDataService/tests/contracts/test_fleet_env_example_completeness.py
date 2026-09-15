"""Contract: fleet credentials are env_file-only, never a compose literal.

`env_file: required: true` is already wired for the live/paper clerk lanes,
but Compose's `environment:` map wins over `env_file:` key-for-key. A literal
credential under `environment:` on any *committed* compose file therefore
defeats the env_file path silently — populating the env files and restarting
produces no observable change, and the next person concludes env_file does
not work.

This guards the fix at the only place it can be verified without touching the
untracked, gitignored `compose.override.yaml` (which is the deployed file,
not a repo artifact): every compose file this repo commits must carry none of
these keys as an inline value, and the example files must document every key
the running coordinator actually needs so a fresh clone can populate them.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RENDER_SCRIPT = ROOT / "scripts" / "render_fleet_topology.py"


def _render_module() -> object:
    """Load scripts/render_fleet_topology.py directly (same dynamic-import
    pattern as test_documentation_contract.py's `_checker_module()`), so its
    Compose-tag-tolerant YAML loader has exactly one implementation."""
    spec = importlib.util.spec_from_file_location("render_fleet_topology", RENDER_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("render_fleet_topology could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SECRET_KEYS = frozenset(
    {
        "FLEET_WORKER_KEY",
        "FLEET_AGENT_SERVICE_TOKEN",
        "FLEET_COORDINATOR_SERVICE_TOKEN",
        "FLEET_AGENT_SERVICE_TOKENS_JSON",
        "FLEET_COORDINATOR_SERVICE_TOKENS_JSON",
        "DATA_PLANE_CONTROL_SECRET",
        "ALPACA_API_KEY_ID",
        "ALPACA_API_SECRET_KEY",
        "POSTGRES_URL",
        "REDIS_URL",
    }
)


def _is_hardcoded_literal(value: str) -> bool:
    """A value containing a `${VAR}` substitution resolves from the shell or
    `.env` at render time — it carries no secret in the committed file. That
    is `compose.yaml`'s long-standing, legitimate pattern (e.g. a templated
    connection string with an embedded `${POSTGRES_PASSWORD}`). The bug this
    test guards is the opposite: a bare, fully-hardcoded value (a 37-char
    token, a JSON token map) that shadows `env_file:` for the same key.
    """
    return value != "" and "${" not in value


def _environment_as_key_value(environment: object) -> dict[str, str]:
    if isinstance(environment, list):
        pairs = (entry.split("=", 1) for entry in environment)
        return {key: (value[0] if value else "") for key, *value in pairs}
    return {str(key): "" if value is None else str(value) for key, value in (environment or {}).items()}


def _example_keys(name: str) -> set[str]:
    text = (ROOT / "deploy/fleet/env" / name).read_text(encoding="utf-8")
    return {
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }


def test_no_committed_compose_file_carries_a_fleet_secret_literal() -> None:
    """Every secret-bearing key is env_file-only or a `${VAR}` passthrough —
    never a hardcoded literal that would shadow `env_file:` for the same key."""
    render_module = _render_module()
    for name in ("compose.yaml", "compose.fleet.yaml", "compose.fleet.dev.yaml"):
        document = render_module.load_compose_document(ROOT / name)
        for service, spec in (document.get("services") or {}).items():
            environment = _environment_as_key_value(spec.get("environment") or {})
            leaked = [
                key for key in SECRET_KEYS & set(environment) if _is_hardcoded_literal(environment[key])
            ]
            assert not leaked, f"{name}:{service} declares secret keys as literals: {sorted(leaked)}"


def test_coordinator_example_declares_every_key_the_running_coordinator_needs() -> None:
    assert {
        "FLEET_AGENT_SERVICE_TOKENS_JSON",
        "FLEET_COORDINATOR_SERVICE_TOKENS_JSON",
        "DATA_PLANE_CONTROL_SECRET",
    } <= _example_keys("coordinator.env.example")


def test_lane_examples_declare_the_four_identity_and_transport_keys() -> None:
    """The four keys the deployed override wrongly re-declares as literals."""
    identity_keys = {
        "FLEET_CLERK_ID",
        "FLEET_WORKER_KEY",
        "FLEET_AGENT_SERVICE_TOKEN",
        "FLEET_COORDINATOR_SERVICE_TOKEN",
    }
    for name in ("live.env.example", "paper.env.example"):
        assert identity_keys <= _example_keys(name), name
