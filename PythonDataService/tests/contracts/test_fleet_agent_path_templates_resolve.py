"""Every catalog agent_path_template resolves to a mounted route (#2076).

The operation catalog is the single routing contract: the coordinator's
forwarding allowlist, the agent's mounts, the exported OpenAPI and the
generated frontend builders all derive from it. Nothing checked that an
agent_path_template names a route the agent actually serves, so
`bots_deploy_apply` declared POST .../bots/deploy against a GET-only mount —
a command the coordinator would persist a routing attempt for, dispatch, and
collect a 405 on.
"""

from __future__ import annotations

import re

from starlette.routing import Match

from app.broker.fleet_composition import production_provider_adapters
from app.main import app

#: Path parameters may carry Starlette converters; `{order_ref:path}` must be
#: filled with something multi-segment or the route under-matches.
_PATH_PARAM = re.compile(r"\{([a-z_][a-z0-9_]*)(?::([a-z]+))?\}")


def _concrete(template: str) -> str:
    return _PATH_PARAM.sub(
        lambda match: "seg/one" if match.group(2) == "path" else "probe-value",
        template,
    )


def _resolves(method: str, path: str) -> bool:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "root_path": "",
        "headers": [],
        "query_string": b"",
    }
    return any(route.matches(scope)[0] is Match.FULL for route in app.routes)


#: A floor, not a pin: the exact catalog size moves as operations are added
#: or retired (this PR alone takes it from 77 to 76), so this test must not
#: hardcode that count. But `unresolved == []` is vacuously true over zero
#: operations — an emptied registry or an adapter declaring no operations
#: would pass this test having probed nothing. The floor is set far below
#: today's count so routine catalog changes never touch it; its only job is
#: to make "nothing was checked" as loud as "one entry didn't resolve"
#: (independent review, 2026-09-14).
_MINIMUM_EXPECTED_OPERATIONS = 10


def test_every_catalog_agent_path_resolves_to_a_mounted_route() -> None:
    operations = [
        operation
        for adapter in production_provider_adapters().values()
        for operation in adapter.operations()
    ]
    assert len(operations) >= _MINIMUM_EXPECTED_OPERATIONS, (
        f"only {len(operations)} catalog operations were found to probe; the "
        "production registry may be empty, which would make the resolution "
        "check below pass having verified nothing"
    )

    unresolved = sorted(
        (operation.operation_id, operation.method, operation.agent_path_template)
        for operation in operations
        if not _resolves(operation.method, _concrete(operation.agent_path_template))
    )

    assert unresolved == [], (
        "each entry declares an agent path this process does not serve at that "
        f"method: {unresolved}"
    )


def test_the_probe_detects_a_method_that_is_not_mounted() -> None:
    """The check would be vacuous if PARTIAL matches counted."""
    assert _resolves("GET", "/api/brokers/alpaca/accounts/x/bots/deploy")
    assert not _resolves("DELETE", "/api/brokers/alpaca/accounts/x/bots/deploy")
