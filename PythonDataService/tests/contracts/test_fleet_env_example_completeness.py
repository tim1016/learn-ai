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

import re

import pytest

from tests.contracts.compose_files import (
    ROOT,
    environment_as_key_value,
    render_module,
    tracked_compose_files,
)

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

LANE_IDENTITY_KEYS = (
    "FLEET_CLERK_ID",
    "FLEET_WORKER_KEY",
    "FLEET_AGENT_SERVICE_TOKEN",
    "FLEET_COORDINATOR_SERVICE_TOKEN",
)


# Matches `${SECRET_KEY:-clause}`, `${SECRET_KEY-clause}`, `${SECRET_KEY:?clause}`
# and `${SECRET_KEY?clause}` for any name in SECRET_KEYS. The operator is
# captured separately from the clause: `:-`/`-` supply a fallback *value* that
# becomes the actual runtime value whenever the variable is unset — hiding a
# real credential there defeats env_file exactly like a bare literal. `:?`/`?`
# supply a diagnostic *message* shown only when a required variable is
# missing (compose.yaml's own `${DATA_PLANE_CONTROL_SECRET:?Set ... before
# starting the stack}`) — that text never becomes the value, so it is exempt.
_SECRET_DEFAULT_PATTERN = re.compile(
    r"\$\{(" + "|".join(re.escape(key) for key in SECRET_KEYS) + r")(:-|-|:\?|\?)([^}]*)\}"
)


def _embeds_secret_default(value: str) -> str | None:
    """Returns the culprit key when `value` embeds a non-empty `${KEY:-...}`
    or `${KEY-...}` fallback for some key in SECRET_KEYS — the bypass of the
    `${VAR}`-passthrough carve-out below: wrapping a real credential as the
    variable's *own* default still ships it in the committed file whenever
    nothing overrides that variable at deploy time.
    """
    for match in _SECRET_DEFAULT_PATTERN.finditer(value):
        key, operator, clause = match.group(1), match.group(2), match.group(3)
        if operator in (":-", "-") and clause.strip():
            return key
    return None


def _is_hardcoded_literal(value: str) -> bool:
    """A value containing a `${VAR}` substitution resolves from the shell or
    `.env` at render time — it carries no secret in the committed file. That
    is `compose.yaml`'s long-standing, legitimate pattern (e.g. a templated
    connection string with an embedded `${POSTGRES_PASSWORD}`). The bug this
    test guards is a bare, fully-hardcoded value (a 37-char token, a JSON
    token map) that shadows `env_file:` for the same key — or that same
    value smuggled in as a variable's own fallback default
    (`${FLEET_WORKER_KEY:-<the secret>}`), which is just as committed.
    """
    if value == "":
        return False
    if _embeds_secret_default(value) is not None:
        return True
    return "${" not in value


def _example_keys(name: str) -> set[str]:
    text = (ROOT / "deploy/fleet/env" / name).read_text(encoding="utf-8")
    return {
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }


@pytest.mark.parametrize(
    ("value", "hardcoded"),
    [
        pytest.param("wkrk_deadbeefdeadbeefdeadbeefdeadbeef", True, id="bare-secret-value"),
        # A real credential wrapped as the variable's own `${KEY:-...}` fallback
        # evades a naive "contains ${" carve-out entirely -- it still ships in the
        # committed file, unconditionally, whenever the variable is unset.
        pytest.param(
            "${FLEET_WORKER_KEY:-sk-live-should-never-ship-1234567890}", True, id="default-clause-bypass-colon-dash"
        ),
        pytest.param("${DATA_PLANE_CONTROL_SECRET-another-hidden-default}", True, id="default-clause-bypass-dash"),
        pytest.param(
            "prefix-${ALPACA_API_SECRET_KEY:-embedded-secret}-suffix", True, id="default-clause-bypass-embedded"
        ),
        # Non-secret defaults (this repo's own compose idiom, e.g.
        # FLEET_DEPLOYMENT_NAMESPACE throughout compose.fleet.dev.yaml), Compose's
        # required-with-message form (compose.yaml's own DATA_PLANE_CONTROL_SECRET
        # usage), a bare passthrough, and a templated connection string whose
        # embedded variable is not itself a secret name must all stay green -- or
        # the fix trades a false negative for a false positive.
        pytest.param("${FLEET_DEPLOYMENT_NAMESPACE:-compose:learn-ai}", False, id="non-secret-default"),
        pytest.param(
            "${DATA_PLANE_CONTROL_SECRET:?Set DATA_PLANE_CONTROL_SECRET in .env before starting the stack}",
            False,
            id="required-with-message",
        ),
        pytest.param("${FLEET_WORKER_KEY}", False, id="bare-passthrough"),
        pytest.param(
            "redis://:${REDIS_PASSWORD:-local-dev-redis-password}@redis:6379/0",
            False,
            id="templated-connection-string-with-non-secret-variable",
        ),
    ],
)
def test_hardcoded_literal_check_catches_secrets_and_leaves_legitimate_interpolation_green(
    value: str, hardcoded: bool
) -> None:
    assert _is_hardcoded_literal(value) is hardcoded


def test_discovery_finds_every_committed_compose_overlay_and_none_carries_a_fleet_secret_literal() -> None:
    """The discovery mechanism itself must cover the file that was previously
    missed — a hardcoded tuple silently omitted it despite this test's own
    docstring promising every committed compose file.

    Every secret-bearing key is env_file-only or a `${VAR}` passthrough —
    never a hardcoded literal that would shadow `env_file:` for the same key."""
    renderer = render_module()
    tracked = tracked_compose_files()
    assert {"compose.yaml", "compose.fleet.yaml", "compose.fleet.dev.yaml",
            "compose.fleet.qualification.yaml"} <= set(tracked), (
        "discovery omits a committed compose file"
    )
    assert tracked, "no tracked compose*.yaml files found — git ls-files may be misconfigured"
    for name in tracked:
        document = renderer.load_compose_document(ROOT / name)
        for service, spec in (document.get("services") or {}).items():
            environment = environment_as_key_value(spec.get("environment") or {})
            leaked = [
                key for key in SECRET_KEYS & set(environment) if _is_hardcoded_literal(environment[key])
            ]
            assert not leaked, f"{name}:{service} declares secret keys as literals: {sorted(leaked)}"


@pytest.mark.parametrize(
    ("example", "required_keys"),
    [
        pytest.param(
            "coordinator.env.example",
            (
                "FLEET_AGENT_SERVICE_TOKENS_JSON",
                "FLEET_COORDINATOR_SERVICE_TOKENS_JSON",
                "DATA_PLANE_CONTROL_SECRET",
            ),
            id="coordinator-declares-every-key-the-running-coordinator-needs",
        ),
        # The four keys the deployed override wrongly re-declares as literals.
        pytest.param("live.env.example", LANE_IDENTITY_KEYS, id="live-lane-declares-the-four-identity-and-transport-keys"),
        pytest.param(
            "paper.env.example", LANE_IDENTITY_KEYS, id="paper-lane-declares-the-four-identity-and-transport-keys"
        ),
    ],
)
def test_example_env_files_declare_every_key_their_role_needs(example: str, required_keys: tuple[str, ...]) -> None:
    assert set(required_keys) <= _example_keys(example), example


def test_lane_examples_document_the_lake_catalog_url_as_dev_only() -> None:
    """#2166: POSTGRES_URL reaches the lanes via env_file only (the compose
    overlay carries no inline value for it), so the examples must document
    the key — but deliberately as a COMMENTED line. The production rollout
    copies these files verbatim and its clerks sit on fleet-private, where
    ``db`` is unreachable: an active URL there would turn the pending
    production lake-wiring decision into silent connection failures. The
    documented value must name the read-only fleet_lake_catalog role,
    never the superuser login compose.yaml hands the combined role."""
    for name in ("live.env.example", "paper.env.example"):
        text = (ROOT / "deploy/fleet/env" / name).read_text(encoding="utf-8")
        assert "POSTGRES_URL" not in _example_keys(name), (
            f"{name}: POSTGRES_URL must stay commented — an active value is "
            "copied verbatim into production, where db is unreachable (#2166 "
            "review)"
        )
        documented = [
            line
            for line in text.splitlines()
            if line.lstrip().startswith("# POSTGRES_URL=")
        ]
        assert len(documented) == 1, f"{name}: expected exactly one documented POSTGRES_URL line"
        url = documented[0].lstrip().lstrip("# ").split("=", 1)[1].strip()
        assert url.startswith("postgres://fleet_lake_catalog:"), (
            f"{name}: the documented lane URL must use the fleet_lake_catalog role"
        )
        assert ":postgres:@" not in url and "//postgres:" not in url, (
            f"{name}: the documented lane URL must not be a superuser login"
        )
