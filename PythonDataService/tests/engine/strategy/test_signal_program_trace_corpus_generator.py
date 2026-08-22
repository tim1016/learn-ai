"""Fidelity tests for the shared golden-corpus generator.

``scripts/generate_signal_program_trace_corpus.py`` is the sole tool that
mints a Signal Program's ``golden_trace_root``, and every program promoted
from issue #1730 onward is qualified against a corpus it produced. A silent
regression in its replay would not fail loudly -- it would mint a *new*
root that then gets committed and pinned as "golden", which is precisely
the anti-pattern ``.claude/rules/numerical-rigor.md`` bans. These tests pin
the generator against a corpus it did not author.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.generate_signal_program_trace_corpus import format_corpus_text, generate_corpus

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/golden"


def _committed(relative: str) -> dict[str, object]:
    return json.loads((_FIXTURES / relative).read_text(encoding="utf-8"))


def test_generator_reproduces_every_committed_ema_per_cell_trace_root() -> None:
    """The independent-authorship check: EMA's corpus was hand-authored
    before this generator existed, so reproducing its per-cell results is a
    real test of the generator's replay math rather than a tautology.

    Per-cell ``trace_root`` is the semantic commitment over one cell's
    ordered ``EvaluationTrace`` payloads, so equality here means the
    generator drives the session through exactly the decisions the pinned
    corpus recorded -- same bars, same warmup, same settings coercion,
    same staging.
    """
    committed = _committed("ema-signal-session/v1/trace-corpus.json")
    generated = generate_corpus("ema_crossover_signal")

    committed_by_cell = {entry["cell"]: entry for entry in committed["entries"]}
    generated_by_cell = {entry["cell"]: entry for entry in generated["entries"]}

    assert set(generated_by_cell) == set(committed_by_cell)
    for cell, entry in committed_by_cell.items():
        assert generated_by_cell[cell]["trace_root"] == entry["trace_root"], (
            f"generator replayed cell {cell!r} to a different trace root than the "
            "committed corpus -- its decision replay has drifted"
        )
        assert generated_by_cell[cell]["trace_count"] == entry["trace_count"]


def test_ema_aggregate_root_stays_the_documented_non_regenerable_exception() -> None:
    """EMA's *aggregate* root is knowingly not reproducible, because
    ``trace_corpus_root`` hashes each entry's ``settings`` text and this
    corpus was hand-authored with the settings as submitted (``"0.20"``)
    rather than as the contract stores them (``0.2``). That reasoning is
    recorded in the corpus's ``attribution.md``.

    This test exists so the exception cannot rot silently in either
    direction: if someone makes EMA regenerable, this fails and points at
    the note that must then be deleted.
    """
    committed = _committed("ema-signal-session/v1/trace-corpus.json")
    generated = generate_corpus("ema_crossover_signal")

    assert generated["trace_root"] != committed["trace_root"]
    attribution = (_FIXTURES / "ema-signal-session/v1/attribution.md").read_text(encoding="utf-8")
    assert "not byte-regenerable" in attribution, (
        "the documented reason for EMA's non-regenerable aggregate root has gone missing"
    )


def test_every_generated_program_corpus_is_byte_identical_to_its_committed_file() -> None:
    """Programs promoted from #1730 onward must stay byte-regenerable.

    This is what makes their golden roots auditable: a reviewer can rerun
    the generator and get the committed bytes back. Deliberately driven off
    the registry rather than a hand-listed set of program keys, so a
    program added in a later wave is covered without editing this test.
    EMA is excluded by the documented exception above, not by omission.
    """
    from app.engine.strategy.registry import _STRATEGY_REGISTRY

    regenerable = {
        key: reg
        for key, reg in _STRATEGY_REGISTRY.items()
        if reg.signal_program_factory is not None and key != "ema_crossover_signal"
    }
    assert regenerable, "expected at least one generator-authored Signal Program corpus"

    for key in regenerable:
        corpus = generate_corpus(key)
        candidates = [
            path
            for path in _FIXTURES.glob("*/v1/trace-corpus.json")
            if json.loads(path.read_text(encoding="utf-8")).get("program_key") == key
        ]
        assert len(candidates) == 1, f"expected exactly one committed corpus for '{key}', found {len(candidates)}"
        assert format_corpus_text(corpus) == candidates[0].read_text(encoding="utf-8"), (
            f"'{key}' corpus is not byte-regenerable -- rerun "
            f"scripts/generate_signal_program_trace_corpus.py --program {key}"
        )
