"""The Strategy Builder's payoff parity fixture is what the canonical answers (#3186).

``Frontend/src/app/utils/strategy-payoff.ts`` is a browser copy of
``app/services/strategy_engine.py``'s max profit, max loss and breakevens
(ADR 0068 exception 1). Its spec reads ``Frontend/src/testing/payoff-parity/cases.json``.
This test regenerates that file from the live canonical and requires the
committed one to match byte for byte, so a hand edit to the fixture, or a change
to the canonical that did not rerun the script, fails here instead of leaving
the browser held to a stale answer.
"""

from __future__ import annotations

from pathlib import Path

from scripts.regenerate_strategy_payoff_parity_fixture import FIXTURE_PATH, render

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_committed_fixture_is_what_the_canonical_produces() -> None:
    assert FIXTURE_PATH == REPOSITORY_ROOT / "Frontend/src/testing/payoff-parity/cases.json"
    assert FIXTURE_PATH.read_text(encoding="utf-8") == render(), (
        "Frontend/src/testing/payoff-parity/cases.json is stale; run "
        '`DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m scripts.regenerate_strategy_payoff_parity_fixture`'
    )
