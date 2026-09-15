"""Structural contract for the unscoped bot-mutation retirement (#2069).

The fleet's own transport is the *account-scoped* surface: every route here
had no catalog operation resolving to it, no frontend caller (Frontend builds
every mutation URL through fleet/clerk-scoped-url.ts) and no script caller.
The two unscoped mutations that remain — the replay-receipt recompute and the
loss-hold clear — are deliberately retained: they are operator routes with no
fleet successor, tracked as stranded in docs/design/fleet-b-route-inventory.md.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.routing import APIRoute

from app.main import app

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_APPLICATION_ROOT = REPOSITORY_ROOT / "Frontend" / "src" / "app"

RETIRED_UNSCOPED_BOT_MUTATIONS = {
    ("POST", "/api/brokers/{broker}/bots"),
    ("POST", "/api/brokers/{broker}/bots/{strategy_instance_id}/stop"),
    ("POST", "/api/brokers/{broker}/bots/{sid}/actions"),
}

RETAINED_STRANDED_UNSCOPED_MUTATIONS = {
    ("POST", "/api/brokers/{broker}/bots/{strategy_instance_id}/runs/{run_id}/replay-receipt"),
    ("POST", "/api/brokers/{broker}/live-envelope/loss-hold/clear"),
}

SUCCESSOR_ACCOUNT_SCOPED_MUTATIONS = {
    ("POST", "/api/brokers/{broker}/accounts/{account_id}/bots"),
    ("POST", "/api/brokers/{broker}/accounts/{account_id}/bots/{sid}/actions"),
}

RETIRED_REQUEST_SCHEMAS = ("DeployBotRequest", "StopBotRequest")

#: Template-literal spellings of the retired unscoped URLs, as this codebase
#: writes them. See the frontend test's docstring for the concatenation gap.
RETIRED_UNSCOPED_URL_LITERALS = ("}/bots`", "}/bots/${", "/bots/stop")


def _registered() -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }


def test_unscoped_bot_mutation_routes_are_absent() -> None:
    registered = _registered()

    assert registered.isdisjoint(RETIRED_UNSCOPED_BOT_MUTATIONS)
    assert registered >= SUCCESSOR_ACCOUNT_SCOPED_MUTATIONS
    assert registered >= RETAINED_STRANDED_UNSCOPED_MUTATIONS


def test_retired_unscoped_deploy_and_stop_bodies_are_absent() -> None:
    """The request shapes had exactly one consumer each; both routes are gone."""
    import app.schemas.broker_bots as broker_bot_schemas

    for name in RETIRED_REQUEST_SCHEMAS:
        assert not hasattr(broker_bot_schemas, name), name


def test_the_frontend_builds_no_unscoped_bot_mutation_url() -> None:
    """Every Frontend mutation goes through fleet/clerk-scoped-url.ts.

    Matches the template-literal spelling this codebase actually writes. A URL
    assembled by concatenation (``scope + '/bots'``) would pass — a real gap,
    demonstrated by mutation rather than assumed, and stated here rather than
    papered over: no such spelling exists in Frontend/src today, and widening
    the match against a shape that does not occur would trade a named blind
    spot for an unexamined one.
    """
    offenders = sorted(
        f"{path.relative_to(FRONTEND_APPLICATION_ROOT)}: {literal}"
        for path, source in (
            (candidate, candidate.read_text(encoding="utf-8"))
            for candidate in FRONTEND_APPLICATION_ROOT.rglob("*.ts")
            if not candidate.name.endswith(".spec.ts")
            and candidate.name != "broker.types.ts"
        )
        for literal in RETIRED_UNSCOPED_URL_LITERALS
        if literal in source
    )

    assert offenders == [], (
        "the Frontend builds a retired unscoped bot-mutation URL; route it "
        f"through fleet/clerk-scoped-url.ts instead: {offenders}"
    )
