"""Phase-taxonomy contract for the canonical Python engine (#471).

The Engine Lab run dock subscribes to SSE ``job.phase`` events and
renders the phase id alongside its friendly label. Drift between the
labels in ``app/jobs/phases.py`` and the ``on_phase(...)`` call sites
in ``app/routers/engine.execute_engine_backtest`` would silently
desync the user-facing run-state chrome from the underlying engine.

These tests pin the contract two ways:

1. The phase registry exposes the agreed taxonomy in the agreed order
   with sane friendly labels.
2. The ``on_phase("...")`` literals inside ``execute_engine_backtest``
   appear in the same order and use no phase ids outside the registry.
   This is a static-source inspection rather than a runtime exercise —
   ``execute_engine_backtest`` requires a registered strategy, a data
   reader, the LEAN data root, and the .NET persistence shim, which
   are too expensive and brittle to mock from a unit test. The static
   check still catches the regression we care about (someone editing
   one place without the other) at well under 1 ms.
"""

from __future__ import annotations

import inspect
import re

from app.jobs.phases import ENGINE_BACKTEST_PHASES, JOB_PHASES, friendly
from app.routers.engine import execute_engine_backtest

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
        # The terminal ``done`` phase is part of the registry (frontend
        # progress-fraction relies on it) but is not emitted by
        # ``on_phase`` — the framework's ``job.completed`` event fills
        # that role. So the on_phase emission sequence is a prefix of
        # the registry ids.
        assert ids[: len(EXPECTED_PHASE_IDS)] == EXPECTED_PHASE_IDS
        assert ids[-1] == "done"

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


class TestExecuteEngineBacktestPhaseSequence:
    """Static-source check: the on_phase emissions inside
    ``execute_engine_backtest`` follow the agreed sequence."""

    def test_on_phase_calls_match_expected_sequence(self) -> None:
        source = inspect.getsource(execute_engine_backtest)
        emitted = re.findall(r'on_phase\("([a-z_]+)"\)', source)
        assert emitted == list(EXPECTED_PHASE_IDS), (
            f"phase emission sequence drifted from the registry; "
            f"saw {emitted!r}, expected {list(EXPECTED_PHASE_IDS)!r}. "
            f"Update both the registry in app/jobs/phases.py and the "
            f"on_phase(...) call sites in app/routers/engine.py together."
        )

    def test_no_legacy_phase_ids_remain(self) -> None:
        """Catch a future edit that re-adds the pre-#471 phase ids."""
        source = inspect.getsource(execute_engine_backtest)
        for legacy in ("loading_bars", "simulating", "computing_stats"):
            assert f'on_phase("{legacy}")' not in source, (
                f"legacy phase id {legacy!r} re-appeared in execute_engine_backtest; "
                f"#471 retired it — use the new taxonomy instead."
            )
