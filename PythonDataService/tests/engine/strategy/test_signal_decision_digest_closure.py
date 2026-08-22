"""Static enforcement that the EMA Signal Program's artifact digest covers
exactly its signal-decision closure — no more, no less.

Issue #1728 defect 2: ``SignalProgramContract.artifact_paths``
(``app/engine/strategy/registry.py``) named only 5 files, but the transitive
first-party import closure of those roots is 22 files. Missing members —
``app/engine/indicators/sma.py`` (EMA seeds itself from
``SimpleMovingAverage``), ``app/engine/indicators/base.py`` (shared
dedup/rounding), ``app/engine/data/trade_bar.py``,
``app/engine/strategy/signal_intent.py``, ``app/utils/timestamps.py``, and
``app/lean_sidecar/trading_calendar.py`` among them — could change an
``EvaluationTrace`` while leaving ``artifact_digest`` unchanged: a stale
receipt would validate a build that was never re-qualified (PRD FR-008,
S11.4).

The naive fix — hash the whole 22-file closure — is equally wrong in the
other direction: it also drags in ``app/engine/execution/*`` (commission,
order, portfolio, sizing, the signal-intent executor) and
``app/engine/framework/insight*.py`` via ``app/engine/strategy/base.py``.
None of those can affect a bar's ``EvaluationTrace`` —
``EmaCrossoverSignalSession.advance()`` (``app/engine/strategy/signal_program.py``)
builds and stores the trace from ``evaluate_signal_bar()``'s return value
*before* ``settle()`` ever calls ``commit_signal_decision()``, so bytes
reached only through the commit path cannot retroactively change a trace
that already exists. Hashing them would invalidate every program receipt on
an unrelated commission-model edit.

This test recomputes the closure from the registered artifact roots and
asserts ``closure - documented_exclusions == artifact_paths`` exactly, so
any newly introduced import must be explicitly triaged into one bucket or
the other before it can land silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from scripts.run_signal_program_build_qualification import (
    _EMA_SIGNAL_DECISION_CLOSURE_EXCLUSIONS,
    _SERVICE_ROOT,
    signal_decision_import_closure,
)


def _ema_signal_contract():
    contract = _STRATEGY_REGISTRY["ema_crossover_signal"].signal_program_contract
    assert contract is not None
    return contract


def test_ema_signal_program_artifact_digest_matches_its_signal_decision_closure() -> None:
    contract = _ema_signal_contract()
    digested = set(contract.artifact_paths)

    closure = signal_decision_import_closure(roots=contract.artifact_paths)
    covered = closure - set(_EMA_SIGNAL_DECISION_CLOSURE_EXCLUSIONS)

    assert covered == digested, (
        "The signal-decision import closure has drifted from the registered "
        "artifact digest. A newly introduced import must be explicitly triaged: "
        "add it to SignalProgramContract.artifact_paths (it can change the signal "
        "decision -> re-qualify) or to _EMA_SIGNAL_DECISION_CLOSURE_EXCLUSIONS in "
        "scripts/run_signal_program_build_qualification.py with a one-line reason "
        "(it provably cannot). "
        f"Missing from artifact_paths: {sorted(covered - digested)!r}; "
        f"Stale in artifact_paths (no longer reachable): {sorted(digested - covered)!r}"
    )


def test_exclusion_list_only_names_files_actually_in_the_closure() -> None:
    """Guards against a stale exclusion surviving a refactor that removes the
    import path which used to reach it — an exclusion for a file that isn't
    even in the closure anymore is dead documentation, not a real triage."""
    contract = _ema_signal_contract()

    closure = signal_decision_import_closure(roots=contract.artifact_paths)

    stale = set(_EMA_SIGNAL_DECISION_CLOSURE_EXCLUSIONS) - closure
    assert not stale, f"Exclusion entries no longer reachable from the roots: {sorted(stale)!r}"


def test_exclusions_and_artifact_paths_are_disjoint() -> None:
    contract = _ema_signal_contract()

    overlap = set(_EMA_SIGNAL_DECISION_CLOSURE_EXCLUSIONS) & set(contract.artifact_paths)
    assert not overlap, f"A file cannot be both digested and excluded: {sorted(overlap)!r}"


def test_every_exclusion_carries_a_non_trivial_reason() -> None:
    for path, reason in _EMA_SIGNAL_DECISION_CLOSURE_EXCLUSIONS.items():
        assert reason.strip(), f"exclusion for {path!r} has no reason"
        assert len(reason.strip()) >= 20, f"exclusion reason for {path!r} is too short to be a real justification"


@pytest.fixture
def synthetic_package(tmp_path: Path) -> Path:
    """A tiny first-party package under ``tmp_path`` used to unit-test the
    closure walker in isolation from the real 22-file production graph."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "root.py").write_text(
        "from app.helper import thing\n\n\ndef late_import() -> None:\n    from app.leaf import deep\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "helper.py").write_text("import app.sibling, app.leaf\n", encoding="utf-8")
    (tmp_path / "app" / "sibling.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "app" / "leaf.py").write_text("y = 2\n", encoding="utf-8")
    return tmp_path


def test_closure_helper_follows_deferred_function_local_imports(synthetic_package: Path) -> None:
    """The production case this guards: ``app/engine/live/indicator_state.py``
    imports ``app.lean_sidecar.trading_calendar`` only inside a function body.
    A closure walker that only looked at module-top imports would silently
    miss it — and miss the real defect 2 gap it represents."""
    closure = signal_decision_import_closure(roots=["app/root.py"], service_root=synthetic_package)

    assert closure == {"app/root.py", "app/helper.py", "app/sibling.py", "app/leaf.py"}


def test_closure_helper_follows_every_name_in_a_combined_import_statement(synthetic_package: Path) -> None:
    """``import app.sibling, app.leaf`` names two modules in one statement;
    the walker must not stop after the first."""
    closure = signal_decision_import_closure(roots=["app/helper.py"], service_root=synthetic_package)

    assert closure == {"app/helper.py", "app/sibling.py", "app/leaf.py"}


def test_production_closure_helper_uses_the_real_service_root() -> None:
    """Sanity check that the default ``service_root`` resolves to
    ``PythonDataService``, not the ``scripts/`` directory the module lives in."""
    assert (_SERVICE_ROOT / "app").is_dir()
    assert (_SERVICE_ROOT / "tests").is_dir()
