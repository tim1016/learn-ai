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

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Match, Route

from app.broker.fleet_composition import production_provider_adapters
from app.main import app

#: Path parameters may carry Starlette converters; each converter only
#: matches values shaped like its own type (`{order_ref:path}` needs
#: something multi-segment, `{id:int}` needs digits, ...), or the route
#: under-matches and a correctly-mounted operation is reported as missing
#: (#2120). Values below are taken straight from each converter's `regex` in
#: `starlette.convertors.CONVERTOR_TYPES`. `str` and untyped params (`None`)
#: fall through to the default.
_PATH_PARAM = re.compile(r"\{([a-z_][a-z0-9_]*)(?::([a-z]+))?\}")

_PROBE_VALUES_BY_CONVERTER: dict[str | None, str] = {
    "path": "seg/one",
    "int": "1",
    "float": "1.5",
    "uuid": "00000000-0000-0000-0000-000000000000",
}


def _concrete(template: str) -> str:
    return _PATH_PARAM.sub(
        lambda match: _PROBE_VALUES_BY_CONVERTER.get(match.group(2), "probe-value"),
        template,
    )


def _scope_for(method: str, path: str) -> dict[str, object]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "root_path": "",
        "headers": [],
        "query_string": b"",
    }


def _resolves(method: str, path: str) -> bool:
    return any(
        route.matches(_scope_for(method, path))[0] is Match.FULL for route in app.routes
    )


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


async def _typed_converter_probe_endpoint(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def _route_matches(template: str, path: str) -> bool:
    route = Route(template, _typed_converter_probe_endpoint, methods=["GET"])
    return route.matches(_scope_for("GET", path))[0] is Match.FULL


@pytest.mark.parametrize(
    "template",
    [
        "/probe/{item_id:int}",
        "/probe/{item_id:float}",
        "/probe/{item_id:uuid}",
    ],
)
def test_concrete_fills_typed_converters_with_a_matching_value(template: str) -> None:
    """#2120: today's catalog has only one typed converter (`:path`), so
    `_concrete()` filling every other parameter with the literal
    `"probe-value"` has never been exercised against `{x:int}` / `{x:uuid}` /
    `{x:float}`. Reproduces the independent reviewer's synthetic-route
    finding directly: mount a route with each converter and prove
    `_concrete()`'s fill both resolves it (the fix, asserted here) and that
    the old one-size-fits-all literal would NOT (asserted in
    `test_the_pre_fix_literal_does_not_satisfy_a_typed_converter` below) — two
    assertions that fail for different reasons, not one checked twice.
    """
    assert _route_matches(template, _concrete(template)), (
        f"_concrete({template!r}) produced a value its own converter rejects"
    )


@pytest.mark.parametrize(
    "template",
    [
        "/probe/{item_id:int}",
        "/probe/{item_id:float}",
        "/probe/{item_id:uuid}",
    ],
)
def test_the_pre_fix_literal_does_not_satisfy_a_typed_converter(template: str) -> None:
    """The other half of the #2120 proof: `"probe-value"` (the pre-fix fill
    for every non-`:path` parameter) must fail these typed converters, or the
    positive assertion above would pass even with the naive fill restored —
    i.e. this is the case that makes a regression back to the untyped fill
    actually redden a test instead of silently under-matching in production.
    """
    naive_path = _PATH_PARAM.sub("probe-value", template)
    assert not _route_matches(template, naive_path), (
        "'probe-value' unexpectedly satisfies this converter, so this "
        f"template ({template!r}) cannot distinguish a converter-aware fill "
        "from the pre-#2120 one-size-fits-all fill"
    )


def test_concrete_still_fills_the_path_converter_with_a_multi_segment_value() -> None:
    """Regression guard for the pre-existing `:path` special case (the
    catalog's real `manual-orders/{order_ref:path}/cancel`): refactoring
    `_concrete()` into a converter map must not lose it. Unlike the other
    converters, `PathConvertor`'s regex (`.*`) also accepts the single-segment
    literal `"probe-value"`, so this one is a regression guard on the
    multi-segment fill itself, not a match/no-match pair.
    """
    template = "/probe/{item_id:path}"
    assert _concrete(template) == "/probe/seg/one"
    assert _route_matches(template, _concrete(template))
