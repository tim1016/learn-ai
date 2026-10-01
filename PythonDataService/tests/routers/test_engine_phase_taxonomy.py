"""Phase-taxonomy contract for the canonical Python engine (#471).

The Engine Lab run dock subscribes to SSE ``job.phase`` events and
renders the phase id alongside its friendly label. Drift between the
labels in ``app/jobs/phases.py`` and the ``on_phase(...)`` call sites
in ``app/services/engine_backtest_service.execute_engine_backtest`` would silently
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

import app.services.engine_backtest_service as engine_module
from app.jobs.phases import ENGINE_BACKTEST_PHASES, friendly
from app.services.engine_backtest_service import (
    _aggregate_backtest_response,
    _execute_engine_backtest_core,
    _persist_and_dispatch_companion,
)

EXPECTED_PHASE_IDS = (
    "fetching_data",
    "consolidating_bars",
    "running_indicators",
    "aggregating_results",
    "persisting",
)


class TestEngineBacktestPhaseRegistry:
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
            f"on_phase(...) call sites in app/services/engine_backtest_service.py together."
        )

    def test_the_workflow_module_emits_no_phase_outside_the_registry(self) -> None:
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
        assert unknown == set(), f"app/services/engine_backtest_service.py emits unregistered phase(s): {sorted(unknown)}"
