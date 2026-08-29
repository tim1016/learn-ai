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
    """The generator must replay EMA to the committed per-cell results.

    This was once the independent-authorship check -- EMA's corpus predated
    the generator, so agreement proved the replay math rather than restating
    it. #1865 moved every trace root (``gap_bps`` entered the evaluation
    identity) and the corpus was regenerated, so that property is gone and
    this now guards against replay drift only.

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


def test_every_generated_program_corpus_is_byte_identical_to_its_committed_file() -> None:
    """Programs promoted from #1730 onward must stay byte-regenerable.

    This is what makes their golden roots auditable: a reviewer can rerun
    the generator and get the committed bytes back. Deliberately driven off
    the registry rather than a hand-listed set of program keys, so a
    program added in a later wave is covered without editing this test.

    EMA used to be excluded: its corpus was hand-authored before the
    generator existed, so its aggregate root was knowingly not re-mintable.
    #1865 put ``gap_bps`` into the evaluation identity, which moved every
    per-cell root in that corpus and invalidated the hand-authored numbers,
    so it was regenerated and now holds to the same standard as the rest.
    The reasoning is recorded in its ``attribution.md``.
    """
    from app.engine.strategy.registry import _STRATEGY_REGISTRY

    regenerable = {
        key: reg
        for key, reg in _STRATEGY_REGISTRY.items()
        if reg.signal_program_factory is not None
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
