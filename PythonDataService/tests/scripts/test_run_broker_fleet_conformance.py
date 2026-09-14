"""Focused checks for Delivery E's bounded conformance evidence ceremony."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import broker_fleet_conformance as conformance
from scripts import run_broker_fleet_conformance as command


def test_selected_node_ids_cover_each_required_delivery_e_claim() -> None:
    """The ceremony stays focused while retaining every Delivery E proof family."""
    checks = {check.check_id: check for check in conformance.CONFORMANCE_CHECKS}

    assert set(checks) == {
        "provider_and_clerk_isolation",
        "capability_and_version_refusal",
        "partial_lane_failure_survivor",
        "per_event_identity",
        "command_uncertainty_never_resubmits",
        "fake_provider_production_boundary",
    }
    assert len(conformance.selected_node_ids()) == len(
        set(conformance.selected_node_ids())
    )
    assert all(check.node_ids for check in checks.values())
    assert (
        "tests/broker/fleet/test_directory_and_aggregation.py::test_partial_aggregation_reports_each_lane_without_omission_or_substitution"
        in checks["partial_lane_failure_survivor"].node_ids
    )
    assert (
        "tests/broker/fleet/test_b_scoped_contracts.py::test_an_older_adapter_build_refuses_registration"
        in checks["capability_and_version_refusal"].node_ids
    )


def test_fake_boundary_refuses_fake_provider_in_a_production_artifact(
    tmp_path: Path,
) -> None:
    """A future composition leak becomes a typed ceremony failure."""
    for relative_path in conformance._PRODUCTION_FAKE_EXCLUSION_PATHS:
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("clean", encoding="utf-8")
    (tmp_path / "compose.fleet.yaml").write_text("fake_alpha", encoding="utf-8")

    with pytest.raises(conformance.FleetConformanceError, match="fake providers"):
        conformance.verify_fake_provider_production_boundary(tmp_path)


def test_targeted_checks_use_one_bounded_pytest_invocation() -> None:
    """The source ceremony cannot accidentally widen into a full-suite command."""
    calls: list[tuple[tuple[str, ...], Path, float]] = []

    def runner(
        invocation: tuple[str, ...], directory: Path, timeout_s: float
    ) -> subprocess.CompletedProcess[str]:
        calls.append((invocation, directory, timeout_s))
        return subprocess.CompletedProcess(invocation, 0, stdout="ok", stderr="")

    result = conformance.run_targeted_code_checks(
        python_executable="python-test", timeout_s=17.0, runner=runner
    )

    assert result["state"] == "passed"
    assert len(calls) == 1
    invocation, directory, timeout_s = calls[0]
    assert invocation[:6] == ("python-test", "-m", "pytest", "-q", "-p", "no:cacheprovider")
    assert directory == conformance.SERVICE_ROOT
    assert timeout_s == 17.0
    assert "tests/broker/fleet" not in invocation
    assert set(invocation[6:]) == set(conformance.selected_node_ids())


def test_evidence_bundle_leaves_human_and_production_gates_required() -> None:
    """A passing code ceremony cannot self-assert operator acceptance."""
    bundle = conformance.build_evidence_bundle(
        code_checks={"state": "passed"},
        fake_provider_boundary={"state": "passed"},
        generated_at_ms=1_789_000_000_000,
    )

    assert bundle["evidence_classification"] == "code_conformance_only"
    layers = bundle["evidence_layers"]
    assert layers["code_checks"]["state"] == "passed"
    assert layers["fake_provider_boundary"]["state"] == "passed"
    assert layers["isolated_qualification"]["state"] == "required"
    assert layers["operator_sign_off"]["state"] == "required"
    assert layers["production_rollout"]["state"] == "required"


def test_main_writes_only_a_code_conformance_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI emits a nonsecret record only after its bounded checks pass."""
    monkeypatch.setattr(
        command,
        "run_targeted_code_checks",
        lambda **_kwargs: {"state": "passed", "checks": []},
    )
    monkeypatch.setattr(
        command,
        "verify_fake_provider_production_boundary",
        lambda: {"state": "passed", "checked_artifacts": []},
    )
    evidence_path = tmp_path / "restricted" / "fleet-e.json"

    assert command.main(["--evidence-path", str(evidence_path), "--timeout-s", "30"]) == 0

    written = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert written["evidence_classification"] == "code_conformance_only"
    assert written["evidence_layers"]["operator_sign_off"]["state"] == "required"
    assert written["evidence_layers"]["production_rollout"]["state"] == "required"


def test_write_evidence_bundle_removes_its_temporary_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed replacement preserves the prior evidence and leaves no temp receipt."""
    evidence_path = tmp_path / "fleet-e.json"
    evidence_path.write_text("prior\n", encoding="utf-8")

    def refusing_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated replacement failure")

    monkeypatch.setattr("app.utils.atomic_file.os.replace", refusing_replace)

    with pytest.raises(OSError, match="simulated"):
        conformance.write_evidence_bundle(evidence_path, {"schema_version": 1})

    assert evidence_path.read_text(encoding="utf-8") == "prior\n"
    assert list(tmp_path.glob(".fleet-e.json.*.tmp")) == []
