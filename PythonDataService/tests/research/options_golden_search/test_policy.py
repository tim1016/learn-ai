"""Qualification gates preserve failures and exact scoped exception acknowledgements."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.research.options_golden_search.policy import classify, validate_exception
from app.research.options_golden_search.protocol import Policy, Window


def metrics(**changes):
    return SimpleNamespace(**({"net_profit_usd": Decimal("1"), "max_drawdown_fraction": Decimal("0.2"),
        "sharpe": 1.0, "sharpe_unavailable_reason": None, "daily_observation_count": 30,
        "completed_traded_sessions": 30, "max_recovery_sessions": 2, "ongoing_recovery": False,
        "technical_failures": ()} | changes))


def window():
    return Window(key="final", start_ms=0, end_ms=100, session_ms=tuple(range(30)),
                  annualization_sessions=252, activity_floor=25)


def test_classify_retains_all_outcomes_and_never_passes_unknown_evidence_without_an_exact_exception():
    assert classify(metrics(), Policy(), window(), "final").eligible, "exact drawdown limit: a result within every rule is eligible"
    fail = classify(metrics(max_drawdown_fraction=Decimal("0.20000000000000000001")), Policy(), window(), "final")
    assert fail.status == "does_not_meet_rules", "exact drawdown limit: a drawdown past the limit does not meet the rules"
    invalid = classify(metrics(net_profit_usd=Decimal("-1"), technical_failures=("held mark missing",)), Policy(), window(), "final")
    assert invalid.status == "could_not_evaluate", "technical precedence: a technical failure makes the verdict could_not_evaluate"
    assert next(o for o in invalid.outcomes if o.rule == "positive_net").status == "does_not_meet_rules", (
        "technical precedence: the failed positive_net rule is still retained among the outcomes"
    )
    # A technical verdict cannot be excepted away.
    with pytest.raises(ValueError, match="technical"):
        validate_exception((invalid,), ("final:technical_validity", "final:positive_net"), "Owner accepts")

    policy = Policy(max_recovery_sessions=3)
    verdict = classify(metrics(ongoing_recovery=True), policy, window(), "final")
    assert verdict.status == "not_enough_evidence", "sparse and ongoing unknown cannot pass: an ongoing recovery is not enough evidence"
    validate_exception((verdict,), ("final:recovery",), "Accept censored recovery")
    # The exception must enumerate its gates and carry a comment.
    with pytest.raises(ValueError, match="enumerate"):
        validate_exception((verdict,), (), "Accept")
    with pytest.raises(ValueError, match="comment"):
        validate_exception((verdict,), ("final:recovery",), " ")
    over = classify(metrics(max_recovery_sessions=4, ongoing_recovery=True), policy, window(), "final")
    assert over.status == "does_not_meet_rules", "sparse and ongoing unknown cannot pass: an ongoing recovery past the limit does not meet the rules"

    verdict = classify(metrics(), Policy(), window(), "pooled_forward", complete=False)
    assert verdict.status == "could_not_evaluate", "a missing fold is technical even if other observations exceed the floor"
    unavailable = classify(metrics(sharpe=None, sharpe_unavailable_reason="constant series"), Policy(), window(), "training")
    assert unavailable.status == "not_enough_evidence", "an unavailable sharpe is not enough evidence"
    assert next(o for o in unavailable.outcomes if o.rule == "sharpe").reason == "constant series", (
        "an unavailable sharpe keeps its reason on the sharpe outcome"
    )
