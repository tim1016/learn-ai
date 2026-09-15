"""Every fleet role's live OpenAPI document agrees with the committed one (#2108).

Issue #2108 claimed the coordinator's two unscoped compatibility reads
(``/api/brokers/{broker}/live-verdict`` and ``/api/brokers/{broker}/panel-profile``,
served by ``app.routers.fleet_compatibility_reads`` only when
``FLEET_ROLE=fleet_coordinator``) were "absent from the committed OpenAPI, so
the frontend is typed against the wrong mount," and proposed restructuring
``scripts/export_openapi_contract.py`` into a union document, one document per
role, or the deployed posture.

Measured, the claim was false: the committed contract (exported under
``FLEET_ROLE=combined``) already describes both paths via the canonical
``brokers`` and ``broker_v2_panel`` routers at the same paths, and the
coordinator's own role document is a **strict subset** of the committed one —
zero paths the committed contract lacks. Exactly two operation objects
differed, and only because both compatibility handlers were annotated
``-> Any``: FastAPI could not infer a response schema, so the coordinator's
own document carried an untyped ``{"title": "..."}`` where the committed
contract (built from the canonical routes' typed handlers) carried a real
``$ref``. Typing the two handlers (``AlpacaLiveVerdict``, ``PanelProfile``)
closed the gap without restructuring the exporter.

This test is the actual fix, not the two-line type annotation: it makes the
general shape of that bug — a fleet role's live document describing a route
differently than the committed contract, or serving a route the committed
contract does not describe at all — a **checked fact** for every future
change, rather than something someone happens to notice while investigating
an issue. Role gating happens at Python import time (``app/main.py`` reads
``FLEET_ROLE`` at module scope), so each role's document is produced by a
real ``app.main`` import in its own subprocess — an in-process
``importlib.reload`` would leave stale role-gated modules in ``sys.modules``
and prove nothing about how the deployed process actually boots.

An independent review found two blind spots in the first version of this
test and proved both with mutations that stayed green:

1. Comparing ``{"$ref": "#/components/schemas/AlpacaLiveVerdict"}`` as an
   opaque string passes even when the two documents define
   ``AlpacaLiveVerdict`` itself differently -- two documents can agree on
   every pointer while disagreeing on everything the pointers point at.
   ``_dereferenced`` below resolves every ``$ref`` against its *own*
   document's ``components.schemas``, recursively, before any comparison
   happens.
2. The response-only comparison never looked at ``parameters`` or
   ``requestBody``, so an added query parameter (or a changed request body
   schema) on a shared route was invisible to it. Both are now compared,
   dereferenced the same way as responses.

A second review (Codex, PR #2135) found three more:

3. The response comparison read only ``content."application/json".schema``,
   so a response with no JSON content -- a different media type, or one with
   its content removed entirely -- normalized to ``None`` on *both* sides
   and compared equal regardless of what actually diverged.
   ``_response_media_schemas`` now compares every media type present on
   either side.
4. The probe used ``setdefault`` for the clerk capacity variables, so an
   environment that explicitly exported ``FLEET_MAX_INFLIGHT_REQUESTS=0``
   (the non-clerk default) kept that ``0`` and crashed the ``clerk_agent``
   import. The probe now assigns them unconditionally: it is constructing
   that role's posture, not inheriting the caller's.
5. ``CHECKED_ROLES`` was a hardcoded tuple. A role added to
   ``FleetSettings.ROLE``'s ``Literal`` without a matching entry here would
   get zero coverage while every test kept passing -- the same failure shape
   as findings 1-2, a guarantee reading broader than what runs.
   ``CHECKED_ROLES`` is now derived from that ``Literal`` directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, get_args

import pytest

from app.config import FleetSettings

#: Each role's document costs a real ``app.main`` import in its own
#: subprocess, which is the whole point (see the probe below) and also makes
#: this module too expensive for the pull-request gate. CI's Python shards
#: already measure 95-120s against a hard 120-second budget
#: (``.github/workflows/ci.yml``), so the two imports here are enough to
#: push a shard over it -- they did, on the first push of #2108. A local
#: measurement does not settle this: the developer machine ran the whole
#: fast gate in 63s where CI needs most of its 120s for the same work.
#: ``run_fast_tests`` selects ``-m "not slow"``, and the scheduled
#: ``daily-tests.yml`` run applies no marker filter, so the coverage is kept
#: rather than dropped (``.claude/rules/testing.md``).
pytestmark = pytest.mark.slow

SERVICE_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = SERVICE_ROOT.parent
COMMITTED_CONTRACT_PATH = REPOSITORY_ROOT / "contracts" / "openapi" / "python-data-service.openapi.json"

#: Every role whose document this test checks -- derived from
#: ``FleetSettings.ROLE``'s own ``Literal``, not hand-maintained. A
#: hardcoded tuple stayed valid, and every test kept passing, the moment a
#: role was added to that ``Literal`` but not copied here: the new role
#: silently got zero coverage and nothing said so (an independent review's
#: third finding on this module -- the same failure shape as the first two:
#: a guarantee that reads broader than what actually runs). ``combined`` is
#: subtracted explicitly, not derived away by accident: it is the role the
#: committed contract itself is exported under (see
#: ``scripts/export_openapi_contract.py``), so it is the reference every
#: other role is compared against, not a second thing to check against
#: itself. A role added to the ``Literal`` without matching support in
#: ``app/main.py`` (or this probe's environment) fails loudly here --
#: ``_export_role_openapi_document``'s subprocess-import assertion --
#: rather than being silently skipped.
_DECLARED_ROLES = frozenset(get_args(FleetSettings.model_fields["ROLE"].annotation))
CHECKED_ROLES = tuple(sorted(_DECLARED_ROLES - {"combined"}))

#: The two compatibility reads this test exists because of. Asserting their
#: presence directly (rather than only asserting "coordinator paths are a
#: subset of committed") is what stops an empty or gutted compatibility
#: router from passing this test vacuously -- a coordinator serving zero
#: paths is trivially a subset of anything.
EXPECTED_COORDINATOR_COMPAT_ROUTES = frozenset(
    {
        ("/api/brokers/{broker}/live-verdict", "get"),
        ("/api/brokers/{broker}/panel-profile", "get"),
    }
)

_HTTP_METHODS = frozenset({"get", "post", "put", "delete", "patch", "options", "head"})

#: The role import probe. Runs in a fresh subprocess because role gating
#: (``_FLEET_ROLE``, ``_ROLE_RUNS_CLERK``, ``_FLEET_COORDINATOR_SURFACE`` in
#: ``app/main.py``) is decided once at module import time -- switching roles
#: within one interpreter would require unwinding every already-imported,
#: role-gated module, which is not how the real coordinator/clerk/combined
#: processes boot. The probe prints exactly one line: the JSON document.
_ROLE_OPENAPI_PROBE = """
import json
import os
import sys

os.environ.setdefault("POLYGON_API_KEY", "contract-schema-placeholder")
os.environ["ALPACA_FAULT_INJECTION_ENABLED"] = "false"
os.environ["FLEET_ROLE"] = sys.argv[1]
# A coordinator needs a non-None control dir to mount its surface at all
# (app/main.py: ``_FLEET_COORDINATOR_SURFACE``); the value is never opened
# during schema export, matching scripts/export_openapi_contract.py's own
# placeholder.
os.environ.setdefault("FLEET_CONTROL_DIR", "contract-schema-fleet-role-probe")
if sys.argv[1] == "clerk_agent":
    # Unconditional, not setdefault: this probe is constructing the
    # clerk_agent posture specifically, so it must not inherit a caller's
    # exported "0" (the non-clerk default) -- that value contradicts the
    # posture being built and app.main raises at import for a non-positive
    # pool size under this role.
    os.environ["FLEET_MAX_INFLIGHT_REQUESTS"] = "4"
    os.environ["FLEET_MAX_INFLIGHT_STREAMS"] = "4"

from app.main import app

sys.stdout.write(json.dumps(app.openapi()))
"""


def _load_committed_contract() -> dict[str, Any]:
    return json.loads(COMMITTED_CONTRACT_PATH.read_text(encoding="utf-8"))


def _export_role_openapi_document(role: str) -> dict[str, Any]:
    """The OpenAPI document a real ``app.main`` import produces for ``role``.

    A subprocess, not an in-process reimport: see the probe's own docstring
    above for why role switching cannot be simulated within one interpreter.
    """
    environment = os.environ.copy()
    result = subprocess.run(
        [sys.executable, "-c", _ROLE_OPENAPI_PROBE, role],
        cwd=SERVICE_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"importing app.main under FLEET_ROLE={role} failed:\n{result.stderr}"
    )
    return json.loads(result.stdout)


def _operations_by_path_method(document: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Map every ``(path, http_method)`` this document serves to its operation object."""
    return {
        (path, method): operation
        for path, methods in document.get("paths", {}).items()
        for method, operation in methods.items()
        if method in _HTTP_METHODS
    }


def _response_media_schemas(response_object: dict[str, Any]) -> dict[str, Any]:
    """Every media type this response declares, mapped to its schema --
    e.g. ``{"application/json": {...}, "text/csv": {...}}`` -- or ``{}`` for
    a response with no body (a 204) or whose content was removed entirely.

    Comparing this whole mapping, instead of reading only the
    ``application/json`` entry, is what catches a response that changed
    media type, dropped a media type, or lost its content altogether: ``{}``
    and ``{"application/json": {...}}`` are different dicts and compare
    unequal, where reading only the JSON key would normalize *both* to the
    same ``None`` (an independent review's finding -- a role diverging on a
    non-JSON response, or dropping a response body, stayed invisible to a
    comparison that only ever looked at one named key).

    Only each media type's schema -- not the sibling ``description`` string
    on the response object -- because ``description`` is FastAPI's
    per-status boilerplate ("Successful Response", "Validation Error") and
    carries no contract information the schema doesn't already carry more
    precisely.
    """
    content = response_object.get("content") or {}
    return {media_type: media_object.get("schema") for media_type, media_object in content.items()}


#: The only ``$ref`` shape FastAPI emits in this service's documents. A ref
#: into ``#/components/responses`` or ``#/components/parameters`` would not
#: match this prefix and would pass through ``_dereferenced`` unresolved;
#: none exist today (every sampled response/parameter/requestBody schema in
#: both the committed contract and every checked role points only into
#: ``components/schemas``), so under-resolving a form that does not occur is
#: not a live gap, but it is why this is a named constant instead of a
#: silent assumption buried in the recursion.
_COMPONENT_SCHEMA_REF_PREFIX = "#/components/schemas/"


def _dereferenced(fragment: Any, document: dict[str, Any], visiting: frozenset[str] = frozenset()) -> Any:
    """Recursively replace every ``{"$ref": "#/components/schemas/Name"}``
    node with ``document``'s own definition of ``Name``.

    Comparing raw fragments treats two documents that reference the same
    schema *name* as agreeing, even when that name is defined differently in
    each document's own ``components.schemas`` -- exactly the blind spot an
    independent review caught (adding a field to ``AlpacaLiveVerdict``
    stayed invisible to a test that only ever compared the pointer string).
    Resolving against each side's own document, recursively, closes that:
    a divergence anywhere in the reachable definition surfaces at the first
    fragment that reaches it.

    ``visiting`` is the chain of schema names currently being expanded on
    *this* recursion path, not a single global set. Component schemas can be
    mutually recursive, so a name that is its own ancestor is a genuine cycle
    and stops there, leaving the raw pointer rather than recursing forever.
    The same name reached again from an unrelated sibling branch is not a
    cycle -- it is expanded independently and in full, so a divergence
    reachable through only one of two occurrences of a shared schema is never
    skipped just because the other occurrence already "used up" the name.
    """
    if isinstance(fragment, dict):
        ref = fragment.get("$ref")
        if isinstance(ref, str) and ref.startswith(_COMPONENT_SCHEMA_REF_PREFIX):
            name = ref[len(_COMPONENT_SCHEMA_REF_PREFIX):]
            if name in visiting:
                return fragment
            target = document.get("components", {}).get("schemas", {}).get(name)
            if target is None:
                return fragment
            return _dereferenced(target, document, visiting | {name})
        return {key: _dereferenced(value, document, visiting) for key, value in fragment.items()}
    if isinstance(fragment, list):
        return [_dereferenced(item, document, visiting) for item in fragment]
    return fragment


def _operation_wire_shape_mismatches(
    *, role: str, path: str, method: str, role_operation: dict[str, Any], role_document: dict[str, Any],
    committed_operation: dict[str, Any], committed_document: dict[str, Any],
) -> list[str]:
    """Every disagreement between one shared path+method's wire contract in
    ``role_document`` and in ``committed_document``: every response's media
    types and their schemas (per status code), ``parameters``, and
    ``requestBody`` -- each resolved against its own document before
    comparing. ``operationId``, ``summary``, ``description`` and ``tags``
    are intentionally excluded (see the module docstring): they name the
    handler and its router grouping, which legitimately differ between a
    compat alias and the canonical route it delegates to, and carry no
    information a caller of the API observes.
    """
    mismatches: list[str] = []

    role_responses = role_operation.get("responses", {})
    committed_responses = committed_operation.get("responses", {})
    for status_code in sorted(set(role_responses) | set(committed_responses)):
        role_media = _dereferenced(_response_media_schemas(role_responses.get(status_code, {})), role_document)
        committed_media = _dereferenced(
            _response_media_schemas(committed_responses.get(status_code, {})), committed_document
        )
        if role_media != committed_media:
            mismatches.append(
                f"{method.upper()} {path} [{status_code}] response media types: "
                f"FLEET_ROLE={role} resolved={role_media!r} != committed resolved={committed_media!r}"
            )

    role_parameters = _dereferenced(role_operation.get("parameters", []), role_document)
    committed_parameters = _dereferenced(committed_operation.get("parameters", []), committed_document)
    if role_parameters != committed_parameters:
        mismatches.append(
            f"{method.upper()} {path} parameters: FLEET_ROLE={role} resolved={role_parameters!r} != "
            f"committed resolved={committed_parameters!r}"
        )

    role_request_body = _dereferenced(role_operation.get("requestBody"), role_document)
    committed_request_body = _dereferenced(committed_operation.get("requestBody"), committed_document)
    if role_request_body != committed_request_body:
        mismatches.append(
            f"{method.upper()} {path} requestBody: FLEET_ROLE={role} resolved={role_request_body!r} != "
            f"committed resolved={committed_request_body!r}"
        )

    return mismatches


@pytest.fixture(scope="module")
def committed_contract() -> dict[str, Any]:
    return _load_committed_contract()


@pytest.fixture(scope="module")
def role_openapi_documents() -> dict[str, dict[str, Any]]:
    """Each checked role's live document, computed once for the whole module.

    Two subprocess ``app.main`` imports total (one per ``CHECKED_ROLES``
    entry), shared across every test function below via this module-scoped
    fixture, rather than once per assertion.
    """
    return {role: _export_role_openapi_document(role) for role in CHECKED_ROLES}


def test_role_documents_are_nonempty_and_the_coordinator_serves_its_compat_routes(
    role_openapi_documents: dict[str, dict[str, Any]],
) -> None:
    """Guards against a vacuous pass. A coordinator (or clerk) document with
    zero paths would trivially satisfy "is a subset of the committed
    contract" -- this asserts each role actually served a real, non-trivial
    surface, and specifically that the coordinator's document contains the
    two compatibility reads this test exists to pin (removing a route from
    ``fleet_compatibility_reads.router`` must fail this, not just shrink a
    subset check that would still pass)."""
    for role, document in role_openapi_documents.items():
        served = set(_operations_by_path_method(document))
        assert served, f"FLEET_ROLE={role} served zero paths -- the import produced an empty app"

    coordinator_served = set(_operations_by_path_method(role_openapi_documents["fleet_coordinator"]))
    missing = EXPECTED_COORDINATOR_COMPAT_ROUTES - coordinator_served
    assert not missing, (
        f"fleet_coordinator no longer serves the compatibility read(s) {missing}; "
        "was a route removed from app/routers/fleet_compatibility_reads.py?"
    )


def test_role_served_paths_are_a_subset_of_the_committed_contract(
    committed_contract: dict[str, Any],
    role_openapi_documents: dict[str, dict[str, Any]],
) -> None:
    """Every path+method a role serves must already be describable from the
    committed contract exported under ``FLEET_ROLE=combined`` -- the
    frontend's codegen only ever reads that one document. A role serving a
    path the committed contract omits would type the frontend against a
    surface it cannot see, which is the failure #2108 (incorrectly) claimed
    already existed."""
    committed_served = set(_operations_by_path_method(committed_contract))

    for role, document in role_openapi_documents.items():
        role_served = set(_operations_by_path_method(document))
        extra = role_served - committed_served
        assert not extra, (
            f"FLEET_ROLE={role} serves path+method(s) the committed contract does not "
            f"describe: {sorted(extra)}. Regenerate contracts/openapi/python-data-service.openapi.json."
        )


def test_shared_operations_agree_with_the_committed_contract(
    committed_contract: dict[str, Any],
    role_openapi_documents: dict[str, dict[str, Any]],
) -> None:
    """For every path+method a role shares with the committed contract, the
    wire contract must be identical: every declared response's media types
    and schemas, every parameter, and the request body -- each with
    ``$ref``s resolved against its own document (see ``_dereferenced``), so
    a same-named component schema that is *defined* differently on the two
    sides cannot hide behind a pointer string that happens to match, and a
    response that lost its body or changed media type cannot hide behind
    two ``None``s that happen to match either.

    Deliberately narrow to that wire contract, not whole operation objects.
    ``operationId``, ``summary``, ``description`` and ``tags`` are
    legitimately different between a compat alias
    (``get_legacy_live_verdict``, tagged ``fleet-compatibility-reads``) and
    the canonical route it delegates to (``get_live_verdict``, tagged
    ``brokers-v2``) -- different function names and different router tags
    are not divergence, they are two names for the same wire contract.
    Comparing whole operation objects would make this test permanently red
    for exactly the routes it exists to protect. Responses, parameters, and
    the request body are what the frontend's codegen actually consumes.
    """
    committed_ops = _operations_by_path_method(committed_contract)

    for role, document in role_openapi_documents.items():
        role_ops = _operations_by_path_method(document)
        shared = set(role_ops) & set(committed_ops)
        assert shared, f"FLEET_ROLE={role} shares no path+method with the committed contract"

        mismatches: list[str] = []
        for path, method in sorted(shared):
            mismatches.extend(
                _operation_wire_shape_mismatches(
                    role=role,
                    path=path,
                    method=method,
                    role_operation=role_ops[(path, method)],
                    role_document=document,
                    committed_operation=committed_ops[(path, method)],
                    committed_document=committed_contract,
                )
            )

        assert not mismatches, "\n".join(mismatches)
