"""The cross-program qualification matrix (PRD issue #1730).

Every registered Signal Program must satisfy the same two invariants: its
committed golden corpus must still replay to the trace root its contract
seals, and its ``artifact_paths`` must equal its real signal-decision import
closure minus an explicitly reasoned exclusion list.

These were previously a near-identical test file per program -- five copies
of the corpus test differing only in a key and a path, and five copies of
the same four closure assertions. That made coverage a matter of who
remembered to copy the file. This is driven off the registry instead, the
way ``test_registry_signal_program_identity.py``,
``test_signal_program_decision_clock.py`` and
``test_signal_program_discard_safety.py`` already are, so a program
promoted later is covered the moment it is registered.

Genuinely program-specific regressions stay in their own files -- EMA's
countdown discard/re-emit behaviour, Strategy C's bar-over-bar ADX state --
because those assert something true of one program only.
"""

from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_program import trace_corpus_root, trace_root
from app.services.spec_strategy_runner import InMemoryDataReader
from scripts.run_signal_program_build_qualification import (
    _CLOSURE_EXCLUSIONS_BY_PROGRAM,
    signal_decision_import_closure,
)

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/golden"
_CELLS = _FIXTURES / "cross-engine-studies/cells"

_SEALED_PROGRAMS = sorted(key for key, reg in _STRATEGY_REGISTRY.items() if reg.signal_program_factory is not None)


def _contract(key: str) -> Any:
    contract = _STRATEGY_REGISTRY[key].signal_program_contract
    assert contract is not None, f"'{key}' has a factory but no contract"
    return contract


def _corpus_path(key: str) -> Path:
    """Locate a program's corpus by the program_key it records.

    Deliberately not derived from the registry key: EMA's fixture directory
    predates the ``<key>-signal`` convention the others follow, and a
    mechanical name would silently miss it.
    """
    matches = [
        path
        for path in _FIXTURES.glob("*/v1/trace-corpus.json")
        if json.loads(path.read_text(encoding="utf-8")).get("program_key") == key
    ]
    assert len(matches) == 1, f"expected exactly one committed corpus for '{key}', found {len(matches)}"
    return matches[0]


@pytest.mark.parametrize("key", _SEALED_PROGRAMS)
def test_validated_settings_corpus_has_a_pinned_trace_root(key: str) -> None:
    """The runtime admission gate (PRD S11.4): a program edit that changes
    behaviour without also updating its sealed ``golden_trace_root`` must
    fail here, not slip through as a "qualified" receipt.
    ``scripts/run_signal_program_build_qualification.py`` binds each emitted
    receipt to this test, so this passing is what makes that receipt
    admissible evidence.
    """
    registration = _STRATEGY_REGISTRY[key]
    contract = _contract(key)
    corpus = json.loads(_corpus_path(key).read_text(encoding="utf-8"))

    assert trace_corpus_root(corpus["entries"]) == corpus["trace_root"]
    assert len(corpus["entries"]) == 10
    assert corpus["trace_root"] == contract.golden_trace_root

    for entry in corpus["entries"]:
        minute_bars: list[TradeBar] = []
        with (_CELLS / entry["cell"] / "lean" / "observations.csv").open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                end_ms = int(row["ms_utc"])
                minute_bars.append(
                    TradeBar(
                        symbol=entry["settings"]["symbol"],
                        start_ms=end_ms - 60_000,
                        end_ms=end_ms,
                        open=Decimal(row["open"]),
                        high=Decimal(row["high"]),
                        low=Decimal(row["low"]),
                        close=Decimal(row["close"]),
                        volume=int(Decimal(row["volume"])),
                    )
                )
        strategy = registration.build(registration.param_schema(**entry["settings"]))
        BacktestEngine(InMemoryDataReader(minute_bars)).run(strategy)
        assert strategy.signal_program is not None
        assert len(strategy.signal_program.session.traces) == entry["trace_count"]
        assert trace_root(strategy.signal_program.session.traces) == entry["trace_root"]


@pytest.mark.parametrize("key", _SEALED_PROGRAMS)
def test_artifact_digest_matches_its_signal_decision_closure(key: str) -> None:
    contract = _contract(key)
    exclusions = _CLOSURE_EXCLUSIONS_BY_PROGRAM[key]
    digested = set(contract.artifact_paths)
    covered = signal_decision_import_closure(roots=contract.artifact_paths) - set(exclusions)

    assert digested == covered, (
        f"'{key}': artifact_paths no longer equals its signal-decision closure. Either "
        "add the file to SignalProgramContract.artifact_paths (it can change the signal "
        "decision -> re-qualify) or give it a reasoned entry in this program's exclusion "
        "list in scripts/run_signal_program_build_qualification.py. "
        f"Missing from artifact_paths: {sorted(covered - digested)!r}; "
        f"Stale in artifact_paths (no longer reachable): {sorted(digested - covered)!r}"
    )


@pytest.mark.parametrize("key", _SEALED_PROGRAMS)
def test_exclusion_list_only_names_files_actually_in_the_closure(key: str) -> None:
    contract = _contract(key)
    stale = set(_CLOSURE_EXCLUSIONS_BY_PROGRAM[key]) - signal_decision_import_closure(roots=contract.artifact_paths)
    assert not stale, f"'{key}' excludes files no longer in its closure: {sorted(stale)!r}"


@pytest.mark.parametrize("key", _SEALED_PROGRAMS)
def test_exclusions_and_artifact_paths_are_disjoint(key: str) -> None:
    contract = _contract(key)
    overlap = set(_CLOSURE_EXCLUSIONS_BY_PROGRAM[key]) & set(contract.artifact_paths)
    assert not overlap, f"'{key}' both digests and excludes: {sorted(overlap)!r}"


@pytest.mark.parametrize("key", _SEALED_PROGRAMS)
def test_every_exclusion_carries_a_non_trivial_reason(key: str) -> None:
    thin = [path for path, reason in _CLOSURE_EXCLUSIONS_BY_PROGRAM[key].items() if len(reason.strip()) < 40]
    assert not thin, f"'{key}' has exclusions with no substantive reason: {thin!r}"
