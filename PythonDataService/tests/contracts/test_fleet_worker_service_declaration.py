"""Contract: every worker service declares its own compose service name.

``FLEET_WORKER_SERVICE`` is the one fact the deployment tells a worker about
itself, and the desk pastes it verbatim into the restart command an operator
runs after Apply. Nothing else in the repo maps a lane to a compose service:
the public fleet directory deliberately projects no deployment detail, so a
wrong declaration cannot be caught anywhere downstream — it just renders a
command that restarts the wrong process (historically the fleet coordinator,
which applies no lane's staged profile at all).

The drift this guards is a service *rename*. Renaming ``alpaca-paper-clerk``
in a compose file while its ``FLEET_WORKER_SERVICE`` value stays behind leaves
the desk confidently printing a command for a service that no longer exists —
silently, because a value-only test still passes. Pinning the value to the
service key it sits under makes that rename a red test.

The coordinator is the other half of the same fact: it is not a worker, has no
staged profile to apply, and must therefore declare nothing at all.
"""

from __future__ import annotations

import yaml

from tests.contracts.compose_files import (
    ROOT,
    environment_as_key_value,
    render_module,
    tracked_compose_files,
)

DECLARATION_KEY = "FLEET_WORKER_SERVICE"

# The combined/legacy posture: one process is both coordinator and worker, and
# `compose.yaml` names it `python-service`. It declares no FLEET_ROLE at all,
# so the role-driven rules below cannot reach it — it is named here instead.
COMBINED_WORKER = ("compose.yaml", "python-service")

# The qualification overlay is never rendered alone: its harness always passes
# `compose.fleet.yaml -f compose.fleet.qualification.yaml`
# (scripts/run_broker_fleet_compose_qualification.py). Like every other fleet
# key it inherits, it re-declares nothing — so what the overlay owes this
# contract is that it leaves each lane's inherited declaration alone.
QUALIFICATION_OVERLAY = "compose.fleet.qualification.yaml"


def _services(name: str) -> dict[str, dict[str, str]]:
    """Every service in one committed compose file, as {service: env}.

    Read per-file and unmerged, exactly as the env-example contract reads
    them: an overlay is reviewed as written, not as one particular `-f`
    combination happens to resolve it.
    """
    document = render_module().load_compose_document(ROOT / name)
    return {
        service: environment_as_key_value(spec.get("environment") or {})
        for service, spec in (document.get("services") or {}).items()
    }


_PLAIN_MAP_TAG = "tag:yaml.org,2002:map"


def _mapping_entry(node: yaml.Node | None, key: str, *, where: str) -> yaml.Node:
    """One value node out of a YAML mapping node, or a readable failure.

    Raw node trees are walked here rather than constructed dicts, and a bare
    `next()` over one reports a missing key as `StopIteration` — which names
    neither the file, the service, nor the key that moved.
    """
    assert isinstance(node, yaml.MappingNode), f"{where} is not a YAML mapping"
    for key_node, value_node in node.value:
        if key_node.value == key:
            return value_node
    raise AssertionError(f"{where} declares no {key!r}")


def _environment_node_tag(name: str, service: str) -> str:
    """The YAML tag on `services.<service>.environment` in one compose file,
    read without construction so a Compose merge tag is visible.
    `load_compose_document` (via `ComposeTagTolerantLoader`) constructs
    `!override`/`!reset` nodes into plain dicts indistinguishable from an
    untagged map, so `_services` above cannot tell the two apart.
    """
    root = yaml.compose((ROOT / name).read_text(encoding="utf-8"), Loader=yaml.SafeLoader)
    services_node = _mapping_entry(root, "services", where=name)
    service_node = _mapping_entry(services_node, service, where=f"{name}:services")
    return _mapping_entry(service_node, "environment", where=f"{name}:{service}").tag


def test_a_declared_worker_service_always_names_its_own_service_key() -> None:
    """Wherever the key appears, its value is the service it sits under. This
    is the rename tripwire, and it holds for any future worker too."""
    for name in tracked_compose_files():
        for service, environment in _services(name).items():
            if not environment.get(DECLARATION_KEY):
                continue
            assert environment[DECLARATION_KEY] == service, (
                f"{name}:{service} declares {DECLARATION_KEY}="
                f"{environment[DECLARATION_KEY]!r}, not its own service name"
            )


def test_every_clerk_agent_declares_a_worker_service() -> None:
    """A lane whose deployment says nothing renders no restart command at all,
    which leaves the operator with a recorded Apply and no way to finish."""
    covered: set[tuple[str, str]] = set()
    for name in tracked_compose_files():
        for service, environment in _services(name).items():
            if environment.get("FLEET_ROLE") != "clerk_agent":
                continue
            assert environment.get(DECLARATION_KEY), (
                f"{name}:{service} names no {DECLARATION_KEY}"
            )
            covered.add((name, service))
    # A role-driven sweep that matched nothing would pass vacuously.
    assert {
        ("compose.fleet.yaml", "alpaca-paper-clerk"),
        ("compose.fleet.yaml", "alpaca-live-clerk"),
        ("compose.fleet.dev.yaml", "alpaca-paper-clerk"),
        ("compose.fleet.dev.yaml", "alpaca-live-clerk"),
    } <= covered


def test_the_combined_posture_worker_declares_itself() -> None:
    """`compose.yaml` runs the worker as `python-service`; the operator's
    restart command there is correct precisely because the two coincide."""
    name, service = COMBINED_WORKER
    assert _services(name)[service].get(DECLARATION_KEY) == service


def test_no_coordinator_names_a_worker_service() -> None:
    """A coordinator has no staged profile to apply. Declaring a service name
    here is how `podman compose restart python-service` became the desk's
    advice in the first place.

    Absent and explicitly empty are the same fact — an overlay that inherits a
    base file's worker name neutralizes it with `""`, the same fence
    `ALPACA_CLERK_DIR` already uses on this role.
    """
    for name in tracked_compose_files():
        for service, environment in _services(name).items():
            if environment.get("FLEET_ROLE") != "fleet_coordinator":
                continue
            assert not environment.get(DECLARATION_KEY), (
                f"{name}:{service} is a coordinator and must name no {DECLARATION_KEY}"
            )


def test_the_fleet_coordinator_fences_off_the_base_files_worker_name() -> None:
    """`compose.fleet.dev.yaml` layers the coordinator role onto
    `compose.yaml`'s combined worker, and Compose merges `environment:`
    key-for-key — so without an explicit fence the coordinator would inherit
    `python-service` and hand an operator the exact wrong restart command.
    """
    assert _services("compose.fleet.dev.yaml")["python-service"][DECLARATION_KEY] == ""


def test_the_qualification_overlay_leaves_each_lanes_declaration_alone() -> None:
    """The overlay declares nothing of its own and lets `compose.fleet.yaml`'s
    declaration through, which are the two ways it could take the lane's
    restart command away: re-declaring the key with a stale value, or tagging
    its `environment:` `!override`/`!reset` so Compose replaces the base map
    wholesale instead of merging key-for-key. The overlay already uses
    `!override` on `depends_on:`/`volumes:` for `fleet-coordinator` in this
    same file, so the second is a live possibility, not a hypothetical.
    """
    for lane in ("alpaca-paper-clerk", "alpaca-live-clerk"):
        assert DECLARATION_KEY not in _services(QUALIFICATION_OVERLAY)[lane], (
            f"{QUALIFICATION_OVERLAY}:{lane} re-declares {DECLARATION_KEY}, a second "
            "place for a service rename to leave a stale name behind"
        )
        assert _environment_node_tag(QUALIFICATION_OVERLAY, lane) == _PLAIN_MAP_TAG, (
            f"{QUALIFICATION_OVERLAY}:{lane} tags its environment: node, so Compose "
            "replaces the base map instead of merging it and the lane loses its "
            f"inherited {DECLARATION_KEY}"
        )


def test_the_declaration_is_never_a_secret_or_an_endpoint() -> None:
    """The value is a bare compose service name — never a URL, a host:port, or
    anything carrying a credential. It ships in a committed file and is
    rendered into the browser."""
    for name in tracked_compose_files():
        for service, environment in _services(name).items():
            declared = environment.get(DECLARATION_KEY)
            if declared is None:
                continue
            assert declared.isascii() and declared == declared.lower(), f"{name}:{service}"
            assert not any(character in declared for character in ":/@ $"), (
                f"{name}:{service} declares {DECLARATION_KEY}={declared!r}, "
                "which is not a bare service name"
            )
