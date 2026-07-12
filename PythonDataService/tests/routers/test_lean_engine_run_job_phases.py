"""Phase-taxonomy contract for the ``lean_engine_run`` job type (#470).

Same shape as ``test_engine_phase_taxonomy.py`` (#471): two checks pin
the contract:

1. The phase registry in ``app/jobs/phases.py`` exposes the agreed
   taxonomy in the agreed order with sane friendly labels.
2. ``run_trusted_sample`` emits the matching ``_emit_phase("...")``
   call literals in order and uses no phase ids outside the registry.

Static-source inspection rather than a runtime exercise. The
orchestrator needs the launcher process running, a fixture-staging
file tree, and the .NET persist shim — too much I/O for a unit-level
contract test. The regression we care about (someone editing the
registry without updating the call sites, or vice versa) is caught at
well under 1 ms here. End-to-end behaviour is exercised by the
existing ``tests/integration/test_lean_engine_polygon_parity.py``
suite and by the manual flow on the Engine Lab page.
"""

from __future__ import annotations

import inspect
import re

from app.jobs.phases import JOB_PHASES, LEAN_ENGINE_RUN_PHASES, friendly
from app.services.lean_sidecar_service import run_trusted_sample

EXPECTED_PHASE_IDS = (
    "staging_data",
    "launching_sidecar",
    "sidecar_running",
    "parsing_results",
    "persisting",
)


class TestLeanEngineRunPhaseRegistry:
    def test_registry_contains_lean_engine_run(self) -> None:
        assert "lean_engine_run" in JOB_PHASES
        assert JOB_PHASES["lean_engine_run"] is LEAN_ENGINE_RUN_PHASES

    def test_phase_ids_in_expected_order(self) -> None:
        ids = tuple(p.id for p in LEAN_ENGINE_RUN_PHASES)
        assert ids[: len(EXPECTED_PHASE_IDS)] == EXPECTED_PHASE_IDS
        # Terminal ``done`` registered for progress-fraction math even
        # though the framework emits ``job.completed`` instead of
        # ``on_phase("done")``.
        assert ids[-1] == "done"

    def test_friendly_labels_are_present_and_sentence_case(self) -> None:
        for phase in LEAN_ENGINE_RUN_PHASES:
            assert phase.label, f"phase {phase.id} has empty friendly label"
            assert phase.label[0].isupper(), (
                f"phase {phase.id} label should be sentence case: {phase.label!r}"
            )

    def test_friendly_lookup_returns_registered_label(self) -> None:
        for phase in LEAN_ENGINE_RUN_PHASES:
            assert friendly("lean_engine_run", phase.id) == phase.label

    def test_sidecar_running_gets_a_heavier_weight(self) -> None:
        """``sidecar_running`` is the opaque chunk where most wall-time
        is spent; the dock uses weight as a fallback when explicit
        progress events aren't emitted. If we ever forget this, the
        progress bar jumps to 50%+ before the LEAN container has even
        finished staging."""
        weights = {p.id: p.weight for p in LEAN_ENGINE_RUN_PHASES}
        assert weights["sidecar_running"] > weights["staging_data"]
        assert weights["sidecar_running"] > weights["persisting"]


class TestRunTrustedSamplePhaseSequence:
    def test_emit_phase_calls_match_expected_sequence(self) -> None:
        source = inspect.getsource(run_trusted_sample)
        emitted = re.findall(r'_emit_phase\("([a-z_]+)"\)', source)
        assert emitted == list(EXPECTED_PHASE_IDS), (
            f"phase emission sequence drifted from the registry; "
            f"saw {emitted!r}, expected {list(EXPECTED_PHASE_IDS)!r}. "
            f"Update both the registry in app/jobs/phases.py and the "
            f"_emit_phase(...) call sites in app/services/lean_sidecar_service.py "
            f"together."
        )

    def test_progress_callbacks_are_keyword_only_and_optional(self) -> None:
        """Existing callers (the trusted-runs router, parity tests)
        invoke ``run_trusted_sample`` positionally with just the
        request. The new progress hooks must stay optional so those
        call sites don't have to change."""
        sig = inspect.signature(run_trusted_sample)
        assert "on_phase" in sig.parameters
        assert "on_log" in sig.parameters
        for name in ("on_phase", "on_log"):
            param = sig.parameters[name]
            assert param.default is None, f"{name} must default to None"
            assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
                f"{name} must be keyword-only so existing positional callers don't break"
            )

    def test_job_wrapper_serializes_dataclass_result_with_jsonable_encoder(self) -> None:
        """``run_trusted_sample`` returns a dataclass, not a Pydantic
        model. The job wrapper must use FastAPI's jsonable encoder so
        Path fields and nested Pydantic DTOs survive the Redis JSON hop."""
        import app.routers.jobs as jobs_router

        source = inspect.getsource(jobs_router.start_lean_engine_run_job)
        assert "jsonable_encoder(result)" in source
        assert "result.model_dump" not in source
