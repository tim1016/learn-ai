"""The 2026-09-29 missed trade, explained by the bot's own decision math (#2639).

EXP-001 replays the live bot's retained IBKR minutes through the canonical EMA
program. On the 14:30 ET bar the crossover was fresh but the gap (0.026) and
RSI (47.3) both failed; on 14:45 the gap and RSI passed but the cross was no
longer fresh. Those are the rows the owner needed a terminal to find.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from app.schemas.decision_explanation import explanation_record
from scripts.fixture_generators.ema_decision_explanation_2026_09_29 import DECISION_CLOSES_MS, replay

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures/golden/strategy-explanation/EXP-001/v1"
# Float64 from exact Decimal indicator math (attribution.md).
ATOL = 1e-9


def test_ema_explanations_match_the_2026_09_29_fixture() -> None:
    minutes = json.loads((FIXTURE_DIR / "input.json").read_text(encoding="utf-8"))["minutes"]
    expected = json.loads((FIXTURE_DIR / "output.json").read_text(encoding="utf-8"))["explanations"]
    by_close = {bar.end_ms: (bar, decision) for bar, decision in replay(minutes)}

    for close_ms, want in zip(DECISION_CLOSES_MS, expected, strict=True):
        bar, decision = by_close[close_ms]
        got = explanation_record(bar, decision)
        assert got is not None
        assert got.bar.model_dump() == want["bar"]
        assert (got.ready, got.holding, got.signal) == (want["ready"], want["holding"], want["signal"])
        keys = sorted(want["values"])
        assert sorted(got.values) == keys
        np.testing.assert_allclose(
            [got.values[key] for key in keys], [want["values"][key] for key in keys], atol=ATOL, rtol=0
        )
        assert [(c.check_id, c.passed) for c in got.checks] == [(c["check_id"], c["passed"]) for c in want["checks"]]
        for check, want_check in zip(got.checks, want["checks"], strict=True):
            if isinstance(want_check["observed"], str):
                assert check.observed == want_check["observed"]
            else:
                np.testing.assert_allclose(check.observed, want_check["observed"], atol=ATOL, rtol=0)


def test_the_14_30_bar_was_a_fresh_cross_that_failed_both_filters() -> None:
    expected = json.loads((FIXTURE_DIR / "output.json").read_text(encoding="utf-8"))["explanations"]
    at_1430, at_1445 = ({c["check_id"]: c for c in bar["checks"]} for bar in expected)

    assert at_1430["fresh_cross"]["passed"] and at_1430["fresh_cross"]["observed"] == "crossed_up"
    assert not at_1430["gap"]["passed"] and round(at_1430["gap"]["observed"], 3) == 0.026
    assert not at_1430["rsi_band"]["passed"] and round(at_1430["rsi_band"]["observed"], 1) == 47.3

    assert not at_1445["fresh_cross"]["passed"] and at_1445["fresh_cross"]["observed"] == "already_above"
    assert at_1445["gap"]["passed"] and at_1445["rsi_band"]["passed"]
