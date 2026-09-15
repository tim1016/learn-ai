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
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = SERVICE_ROOT.parent
COMMITTED_CONTRACT_PATH = REPOSITORY_ROOT / "contracts" / "openapi" / "python-data-service.openapi.json"

#: Every role whose document this test can produce from a clean import.
#: ``combined`` is what the committed contract itself is exported under
#: (see ``scripts/export_openapi_contract.py``), so it is the reference, not
#: a second thing to check. A ``combined`` role fixture only ever
#: benchmarks the exporter, not the roles.
CHECKED_ROLES = ("fleet_coordinator", "clerk_agent")

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
    os.environ.setdefault("FLEET_MAX_INFLIGHT_REQUESTS", "4")
    os.environ.setdefault("FLEET_MAX_INFLIGHT_STREAMS", "4")

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


def _response_json_schema(response_object: dict[str, Any]) -> Any:
    """The JSON-body schema a response declares, or ``None`` for e.g. 204s.

    Only the schema -- not the sibling ``description`` string -- because
    ``description`` is FastAPI's per-status boilerplate ("Successful
    Response", "Validation Error") and carries no contract information the
    schema doesn't already carry more precisely.
    """
    content = response_object.get("content") or {}
    json_content = content.get("application/json")
    if json_content is None:
        return None
    return json_content.get("schema")


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


def test_shared_response_schemas_agree_with_the_committed_contract(
    committed_contract: dict[str, Any],
    role_openapi_documents: dict[str, dict[str, Any]],
) -> None:
    """For every path+method a role shares with the committed contract, every
    declared response's JSON schema must be identical.

    Deliberately narrow to response schemas. ``operationId``, ``summary``,
    ``description`` and ``tags`` are legitimately different between a compat
    alias (``get_legacy_live_verdict``, tagged ``fleet-compatibility-reads``)
    and the canonical route it delegates to (``get_live_verdict``, tagged
    ``brokers-v2``) -- different function names and different router tags are
    not divergence, they are two names for the same wire contract. Comparing
    whole operation objects would make this test permanently red for exactly
    the routes it exists to protect. The response *schema* is the part the
    frontend's codegen actually consumes.
    """
    committed_ops = _operations_by_path_method(committed_contract)

    for role, document in role_openapi_documents.items():
        role_ops = _operations_by_path_method(document)
        shared = set(role_ops) & set(committed_ops)
        assert shared, f"FLEET_ROLE={role} shares no path+method with the committed contract"

        mismatches: list[str] = []
        for path, method in sorted(shared):
            role_responses = role_ops[(path, method)].get("responses", {})
            committed_responses = committed_ops[(path, method)].get("responses", {})
            for status_code in sorted(set(role_responses) | set(committed_responses)):
                role_schema = _response_json_schema(role_responses.get(status_code, {}))
                committed_schema = _response_json_schema(committed_responses.get(status_code, {}))
                if role_schema != committed_schema:
                    mismatches.append(
                        f"{method.upper()} {path} [{status_code}]: "
                        f"FLEET_ROLE={role} schema={role_schema!r} != "
                        f"committed schema={committed_schema!r}"
                    )

        assert not mismatches, "\n".join(mismatches)
