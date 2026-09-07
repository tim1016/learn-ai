"""Phase-taxonomy contract for the canonical Python engine (#471).

The Engine Lab run dock subscribes to SSE ``job.phase`` events and
renders the phase id alongside its friendly label. Drift between the
labels in ``app/jobs/phases.py`` and the ``on_phase(...)`` call sites
in ``app/routers/engine.execute_engine_backtest`` would silently
desync the user-facing run-state chrome from the underlying engine.

These tests pin the contract two ways:

1. The phase registry exposes the agreed taxonomy in the agreed order
   with sane friendly labels.
2. The ``on_phase("...")`` literals across the workflow's three stages
   (``_execute_engine_backtest_core`` → ``_aggregate_backtest_response`` →
   ``_persist_and_dispatch_companion``) appear in the same order and use no
   phase ids outside the registry.
   This is a static-source inspection rather than a runtime exercise —
   the workflow requires a registered strategy, a data
   reader, the LEAN data root, and the .NET persistence shim, which
   are too expensive and brittle to mock from a unit test. The static
   check still catches the regression we care about (someone editing
   one place without the other) at well under 1 ms.
"""

from __future__ import annotations

import inspect
import re

import app.routers.engine as engine_module
from app.jobs.phases import ENGINE_BACKTEST_PHASES, JOB_PHASES, friendly
from app.routers.engine import (
    _aggregate_backtest_response,
    _execute_engine_backtest_core,
    _persist_and_dispatch_companion,
    _report_waiting,
)

EXPECTED_PHASE_IDS = (
    "fetching_data",
    "consolidating_bars",
    "running_indicators",
    "aggregating_results",
    "persisting",
)


class TestEngineBacktestPhaseRegistry:
    def test_registry_contains_engine_backtest(self) -> None:
        assert "engine_backtest" in JOB_PHASES
        assert JOB_PHASES["engine_backtest"] is ENGINE_BACKTEST_PHASES

    def test_phase_ids_in_expected_order(self) -> None:
        ids = tuple(p.id for p in ENGINE_BACKTEST_PHASES)
        # Two registry ids bracket the run and are not part of the workflow's
        # own emission sequence. ``waiting_for_engine`` leads it but fires only
        # when the process-wide engine gate is already held (#1957), so a run
        # that starts straight away never emits it. The terminal ``done`` is in
        # the registry because the frontend progress-fraction relies on it, but
        # the framework's ``job.completed`` event fills that role instead. What
        # remains between them is the workflow sequence.
        assert ids == ("waiting_for_engine", *EXPECTED_PHASE_IDS, "done")

    def test_friendly_labels_are_present_and_sentence_case(self) -> None:
        for phase in ENGINE_BACKTEST_PHASES:
            assert phase.label, f"phase {phase.id} has empty friendly label"
            # Sentence case: first character is uppercase. The remaining
            # text may include lowercase words; we don't enforce a strict
            # style because some labels include proper nouns (LEAN).
            assert phase.label[0].isupper(), (
                f"phase {phase.id} label should be sentence case: {phase.label!r}"
            )

    def test_friendly_lookup_returns_registered_label(self) -> None:
        for phase in ENGINE_BACKTEST_PHASES:
            assert friendly("engine_backtest", phase.id) == phase.label

    def test_unregistered_phase_falls_back_to_humanized_form(self) -> None:
        # ``_humanize`` capitalizes tokens it sees in lowercase; an
        # unknown phase id should pass through that codepath.
        assert friendly("engine_backtest", "no_such_phase") == "No Such Phase"


# The workflow's three stages, in the order a run walks them. The phase
# emissions used to sit in one 421-line function and were scanned as one body;
# the split (#1991) means the ordering scan follows the call order instead.
# This list is for order only — membership is checked over the whole module,
# so a stage missing from here cannot hide an unregistered phase.
WORKFLOW_STAGES = (
    _execute_engine_backtest_core,
    _aggregate_backtest_response,
    _persist_and_dispatch_companion,
)


class TestExecuteEngineBacktestPhaseSequence:
    """Static-source check: the on_phase emissions across the workflow's stages
    follow the agreed sequence."""

    def test_on_phase_calls_match_expected_sequence(self) -> None:
        emitted = [
            phase
            for stage in WORKFLOW_STAGES
            for phase in re.findall(r'on_phase\("([a-z_]+)"\)', inspect.getsource(stage))
        ]
        assert emitted == list(EXPECTED_PHASE_IDS), (
            f"phase emission sequence drifted from the registry; "
            f"saw {emitted!r}, expected {list(EXPECTED_PHASE_IDS)!r}. "
            f"Update both the registry in app/jobs/phases.py and the "
            f"on_phase(...) call sites in app/routers/engine.py together."
        )

    def test_the_router_emits_no_phase_outside_the_registry(self) -> None:
        """Membership, scanned over the whole module rather than the listed stages.

        ``WORKFLOW_STAGES`` is hand-maintained, which is fine for the *ordering*
        check above — the order is the thing a human has to state. It is the
        wrong basis for membership: a helper nobody added to the tuple could
        emit a phase nobody registered, and every test here would still pass.
        The module has six ``on_phase`` literals in total (the five workflow
        stages plus the gate's), so scanning all of it costs nothing and cannot
        rot.
        """
        registered = {phase.id for phase in ENGINE_BACKTEST_PHASES}
        emitted = set(re.findall(r'on_phase\("([a-z_]+)"\)', inspect.getsource(engine_module)))
        unknown = emitted - registered
        assert unknown == set(), f"app/routers/engine.py emits unregistered phase(s): {sorted(unknown)}"

    def test_the_gate_reports_the_wait_before_the_workflow_starts(self) -> None:
        """``waiting_for_engine`` is the gate's, not the workflow's (#1957)."""
        assert 'on_phase("waiting_for_engine")' in inspect.getsource(_report_waiting)
        for stage in WORKFLOW_STAGES:
            assert "waiting_for_engine" not in inspect.getsource(stage)

    def test_the_wait_log_line_comes_from_the_registry_not_a_second_copy(self) -> None:
        """One label per phase id — a hand-written duplicate is how the two drift."""
        source = inspect.getsource(_report_waiting)
        assert 'friendly("engine_backtest", "waiting_for_engine")' in source
        assert friendly("engine_backtest", "waiting_for_engine") == "Waiting for the backtest already running"

    def test_no_legacy_phase_ids_remain(self) -> None:
        """Catch a future edit that re-adds the pre-#471 phase ids."""
        source = "".join(inspect.getsource(stage) for stage in WORKFLOW_STAGES)
        for legacy in ("loading_bars", "simulating", "computing_stats"):
            assert f'on_phase("{legacy}")' not in source, (
                f"legacy phase id {legacy!r} re-appeared in the engine workflow; "
                f"#471 retired it — use the new taxonomy instead."
            )
