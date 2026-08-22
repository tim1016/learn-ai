"""Static enforcement that the SMA Signal Program's artifact digest covers
exactly its signal-decision closure — no more, no less.

Mirrors ``tests/engine/strategy/test_signal_decision_digest_closure.py``'s
EMA-specific checks (issue #1728 defect 2) for ``sma_crossover``'s own
``SignalProgramContract.artifact_paths`` and
``_SMA_SIGNAL_DECISION_CLOSURE_EXCLUSIONS``. See that module's docstring for
the full defect-2 rationale; this file only re-asserts the same closure
invariant against SMA's own roots. The generic closure-walker unit tests
(deferred imports, combined import statements, service-root resolution)
already live there and are not duplicated here.
"""

from __future__ import annotations

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from scripts.run_signal_program_build_qualification import (
    _SMA_SIGNAL_DECISION_CLOSURE_EXCLUSIONS,
    signal_decision_import_closure,
)


def _sma_signal_contract():
    contract = _STRATEGY_REGISTRY["sma_crossover"].signal_program_contract
    assert contract is not None
    return contract


def test_sma_signal_program_artifact_digest_matches_its_signal_decision_closure() -> None:
    contract = _sma_signal_contract()
    digested = set(contract.artifact_paths)

    closure = signal_decision_import_closure(roots=contract.artifact_paths)
    covered = closure - set(_SMA_SIGNAL_DECISION_CLOSURE_EXCLUSIONS)

    assert covered == digested, (
        "The signal-decision import closure has drifted from the registered "
        "artifact digest. A newly introduced import must be explicitly triaged: "
        "add it to SignalProgramContract.artifact_paths (it can change the signal "
        "decision -> re-qualify) or to _SMA_SIGNAL_DECISION_CLOSURE_EXCLUSIONS in "
        "scripts/run_signal_program_build_qualification.py with a one-line reason "
        "(it provably cannot). "
        f"Missing from artifact_paths: {sorted(covered - digested)!r}; "
        f"Stale in artifact_paths (no longer reachable): {sorted(digested - covered)!r}"
    )


def test_exclusion_list_only_names_files_actually_in_the_closure() -> None:
    """Guards against a stale exclusion surviving a refactor that removes the
    import path which used to reach it — an exclusion for a file that isn't
    even in the closure anymore is dead documentation, not a real triage."""
    contract = _sma_signal_contract()

    closure = signal_decision_import_closure(roots=contract.artifact_paths)

    stale = set(_SMA_SIGNAL_DECISION_CLOSURE_EXCLUSIONS) - closure
    assert not stale, f"Exclusion entries no longer reachable from the roots: {sorted(stale)!r}"


def test_exclusions_and_artifact_paths_are_disjoint() -> None:
    contract = _sma_signal_contract()

    overlap = set(_SMA_SIGNAL_DECISION_CLOSURE_EXCLUSIONS) & set(contract.artifact_paths)
    assert not overlap, f"A file cannot be both digested and excluded: {sorted(overlap)!r}"


def test_every_exclusion_carries_a_non_trivial_reason() -> None:
    for path, reason in _SMA_SIGNAL_DECISION_CLOSURE_EXCLUSIONS.items():
        assert reason.strip(), f"exclusion for {path!r} has no reason"
        assert len(reason.strip()) >= 20, f"exclusion reason for {path!r} is too short to be a real justification"
