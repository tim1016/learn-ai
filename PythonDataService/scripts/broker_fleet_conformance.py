"""Build bounded, machine-readable evidence for fleet provider conformance.

Delivery E needs a repeatable source-checkout ceremony for the Phase 6 fake
provider boundary.  This module deliberately does *not* qualify production
mounts, upstreams, credentials, or an operator decision.  It records those
as separately required evidence classes so a passing test run cannot be
mistaken for rollout acceptance.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.utils.atomic_file import atomic_write_bytes

SERVICE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SERVICE_ROOT.parent
EVIDENCE_SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_S = 90.0


class FleetConformanceError(RuntimeError):
    """The bounded conformance ceremony could not produce trustworthy evidence."""


@dataclass(frozen=True, slots=True)
class ConformanceCheck:
    """One named safety claim and the focused regression test that exercises it."""

    check_id: str
    description: str
    node_ids: tuple[str, ...]


# These are intentionally a focused consumer set, not ``tests/broker/fleet``
# or the full service suite.  The test fixtures already own fake_alpha and
# fake_beta; this ceremony reuses that N-clerk evidence instead of creating a
# second fake topology in an operational script.
CONFORMANCE_CHECKS = (
    ConformanceCheck(
        check_id="provider_and_clerk_isolation",
        description="Two fake providers and N clerks retain separate adapters, volumes, and state.",
        node_ids=(
            "tests/broker/fleet/test_provider_conformance.py::test_n_clerks_across_two_providers_run_concurrently",
            "tests/broker/fleet/test_provider_conformance.py::test_provider_clients_and_state_share_no_mutable_object",
        ),
    ),
    ConformanceCheck(
        check_id="capability_and_version_refusal",
        description="Undeclared capabilities and incompatible protocol or adapter versions fail closed.",
        node_ids=(
            "tests/broker/fleet/test_provider_conformance.py::test_undeclared_capabilities_refuse_with_evidence_not_emulation",
            "tests/broker/fleet/test_b_scoped_contracts.py::test_incompatible_protocol_versions_refuse_registration",
            "tests/broker/fleet/test_b_scoped_contracts.py::test_an_older_adapter_build_refuses_registration",
        ),
    ),
    ConformanceCheck(
        check_id="partial_lane_failure_survivor",
        description="Corrupting one fake lane leaves the correctly identified survivor usable.",
        node_ids=(
            "tests/broker/fleet/test_provider_conformance.py::test_killing_one_clerks_volume_does_not_mutate_another",
            "tests/broker/fleet/test_directory_and_aggregation.py::test_partial_aggregation_reports_each_lane_without_omission_or_substitution",
        ),
    ),
    ConformanceCheck(
        check_id="per_event_identity",
        description="Every routed stream event carries verified broker, clerk, epoch, and binding provenance.",
        node_ids=(
            "tests/broker/fleet/test_b_scoped_contracts.py::test_stream_events_carry_and_verify_provenance",
            "tests/broker/fleet/test_b_scoped_contracts.py::test_a_stale_identity_midstream_closes_the_stream",
        ),
    ),
    ConformanceCheck(
        check_id="command_uncertainty_never_resubmits",
        description="Outcome uncertainty reconciles by the original command identity and never auto-resubmits.",
        node_ids=(
            "tests/broker/fleet/test_routing_attempts.py::test_the_attempt_lifecycle_covers_every_outcome",
            "tests/broker/fleet/test_b_scoped_contracts.py::test_a_settled_attempt_never_redispatches",
        ),
    ),
    ConformanceCheck(
        check_id="fake_provider_production_boundary",
        description="Test fakes stay outside production composition and the committed OpenAPI contract.",
        node_ids=(
            "tests/broker/fleet/test_provider_conformance.py::test_test_fakes_are_absent_from_production_composition_and_openapi",
        ),
    ),
)

_PRODUCTION_FAKE_EXCLUSION_PATHS = (
    Path("compose.yaml"),
    Path("compose.fleet.yaml"),
    Path("PythonDataService/app/broker/fleet_composition.py"),
    Path("contracts/openapi/python-data-service.openapi.json"),
)
_TEST_FAKE_PROVIDER_IDS = ("fake_alpha", "fake_beta")

Runner = Callable[[Sequence[str], Path, float], subprocess.CompletedProcess[str]]


def selected_node_ids() -> tuple[str, ...]:
    """Return the unique focused pytest node IDs in evidence order."""
    return tuple(node_id for check in CONFORMANCE_CHECKS for node_id in check.node_ids)


def verify_fake_provider_production_boundary(
    repository_root: Path = REPOSITORY_ROOT,
) -> dict[str, object]:
    """Prove test fake names are absent from production composition and OpenAPI.

    This intentionally examines only production artifacts.  The qualification
    Compose overlay is allowed to contain external fake upstreams; it is not a
    production composition and cannot be substituted for this boundary check.
    """
    offenders: dict[str, list[str]] = {}
    checked_paths: list[str] = []
    for relative_path in _PRODUCTION_FAKE_EXCLUSION_PATHS:
        path = repository_root / relative_path
        if not path.is_file():
            raise FleetConformanceError(
                f"Required production artifact is missing: {relative_path}."
            )
        text = path.read_text(encoding="utf-8")
        found = [provider_id for provider_id in _TEST_FAKE_PROVIDER_IDS if provider_id in text]
        if found:
            offenders[str(relative_path)] = found
        checked_paths.append(str(relative_path))
    if offenders:
        raise FleetConformanceError(
            "Test-only fake providers appeared in a production artifact: "
            f"{json.dumps(offenders, sort_keys=True)}."
        )
    return {
        "state": "passed",
        "checked_artifacts": checked_paths,
        "excluded_provider_ids": list(_TEST_FAKE_PROVIDER_IDS),
    }


def _subprocess_runner(
    command: Sequence[str], working_directory: Path, timeout_s: float
) -> subprocess.CompletedProcess[str]:
    """Run the focused pytest selection without inheriting a broad test command."""
    return subprocess.run(
        list(command),
        cwd=working_directory,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )


def run_targeted_code_checks(
    *,
    python_executable: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    runner: Runner = _subprocess_runner,
) -> dict[str, object]:
    """Run precisely the provider-conformance regression selection once."""
    if not 0 < timeout_s <= 120:
        raise FleetConformanceError("timeout_s must be greater than zero and at most 120 seconds.")
    command = (
        python_executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        *selected_node_ids(),
    )
    try:
        completed = runner(command, SERVICE_ROOT, timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise FleetConformanceError(
            f"Focused provider conformance exceeded its {timeout_s:g}s bound."
        ) from exc
    if completed.returncode != 0:
        raise FleetConformanceError(
            "Focused provider-conformance regressions failed. Run the listed node IDs "
            "directly for their diagnostic output."
        )
    return {
        "state": "passed",
        "command": list(command),
        "working_directory": str(SERVICE_ROOT),
        "timeout_s": timeout_s,
        "checks": [
            {
                "id": check.check_id,
                "description": check.description,
                "pytest_node_ids": list(check.node_ids),
            }
            for check in CONFORMANCE_CHECKS
        ],
    }


def build_evidence_bundle(
    *,
    code_checks: dict[str, object],
    fake_provider_boundary: dict[str, object],
    generated_at_ms: int | None = None,
) -> dict[str, object]:
    """Build the Delivery E evidence record without claiming human acceptance.

    The three non-code layers deliberately remain ``required``.  A source
    checkout has no authority to decide an operator sign-off or production
    rollout, and fake-provider evidence cannot qualify real mounts or brokers.
    """
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "generated_at_ms": int(time.time() * 1000) if generated_at_ms is None else generated_at_ms,
        "delivery": "E",
        "evidence_classification": "code_conformance_only",
        "evidence_layers": {
            "code_checks": code_checks,
            "fake_provider_boundary": fake_provider_boundary,
            "isolated_qualification": {
                "state": "required",
                "required_evidence": "Delivery D isolated actual-role Compose qualification record",
                "not_proved_here": [
                    "physical production mount separation",
                    "production credential injection",
                    "real broker or market-data upstream behavior",
                ],
            },
            "operator_sign_off": {
                "state": "required",
                "required_evidence": "named operator decision and dated restricted acceptance record",
                "not_proved_here": [
                    "operator acceptance",
                    "Live command authorization",
                ],
            },
            "production_rollout": {
                "state": "required",
                "required_evidence": "reviewed production qualification and rollout record",
                "not_proved_here": [
                    "deployment qualification",
                    "Paper canary qualification",
                    "Live read-only qualification",
                    "operational rollout",
                ],
            },
        },
    }


def write_evidence_bundle(path: Path, bundle: dict[str, object]) -> None:
    """Durably write a complete nonsecret JSON evidence record.

    A result here is an operator receipt reference, not an expendable cache:
    flush the replacement file, atomically replace the prior record, then
    flush the containing directory.  A failed write removes its temporary
    sibling without touching any previously complete record.
    """
    encoded = (json.dumps(bundle, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write_bytes(path, encoded)
