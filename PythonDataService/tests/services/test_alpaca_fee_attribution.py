"""Independent conservation cases for PRD #2540; Decimal equality is exact."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.broker.contract.models import OrderSide
from app.services.alpaca_fee_attribution import FeeCharge, FeeFill, apportion_cents, attribute_session_fees

D = Decimal
DAY = date(2026, 9, 8)


def _fill(subject: str, *, side=OrderSide.SELL, quantity="1", price="1", fee=None) -> FeeFill:
    return FeeFill(
        subject,
        subject,
        side,
        D(quantity),
        D(price),
        native_order_id=f"order-{subject}",
        reported_fee=fee,
        observed_at_ms=10,
    )


def _attribute(fills=(), charges=(), **kwargs):
    return attribute_session_fees(
        trade_date=DAY,
        fills=fills,
        charges=charges,
        population_complete=kwargs.pop("population_complete", True),
        activities_complete=kwargs.pop("activities_complete", True),
        **kwargs,
    )


def test_equal_weights_split_five_cents_by_stable_subject_not_input_order() -> None:
    assert apportion_cents(D("0.05"), {"bot:c": D(1), "bot:a": D(1), "bot:b": D(1)}) == {
        "bot:a": D("0.02"),
        "bot:b": D("0.02"),
        "bot:c": D("0.01"),
    }


def test_fractional_exact_remainder_does_not_round_each_subject_up() -> None:
    assert apportion_cents(D("0.01"), {"a": D("0.00000000000000000000001"), "b": D("0.00000000000000000000002")}) == {
        "a": D("0"),
        "b": D("0.01"),
    }


def test_component_weights_use_sells_only_for_sec_and_both_sides_for_cat() -> None:
    fills = [_fill("buyer", side=OrderSide.BUY, quantity="100"), _fill("seller", quantity="1", price="100")]
    sec = _attribute(fills, [FeeCharge("sec", D("0.10"), 100, component="sec")])
    cat = _attribute(fills, [FeeCharge("cat", D("0.10"), 100, component="cat")])
    assert sec.total_for("seller") == D("0.10") and sec.total_for("buyer") == 0
    assert cat.total_for("buyer") == D("0.10") and cat.total_for("seller") == 0


def test_same_symbol_bot_and_manual_population_get_separate_exact_shares() -> None:
    result = _attribute(
        [_fill("bot:a"), _fill("manual-operator:owner"), _fill("bot:b")], [FeeCharge("charge", D("0.05"), 100)]
    )
    assert result.known
    assert {share.subject_id: share.amount for share in result.shares} == {
        "bot:a": D("0.02"),
        "bot:b": D("0.02"),
        "manual-operator:owner": D("0.01"),
    }
    assert sum((share.amount for share in result.shares), D(0)) == result.observed_total == D("0.05")


def test_observed_settlement_replaces_pending_not_added_to_it() -> None:
    pending = _attribute([_fill("a")])
    observed = _attribute([_fill("a")], [FeeCharge("settled", D("0.05"), 100)])
    assert pending.total_for("a") == D("0.03")
    assert {share.state for share in pending.shares} == {"estimated"}
    assert observed.total_for("a") == D("0.05")
    assert {share.state for share in observed.shares} == {"observed"}
    assert observed.unobserved_cash_claim(cash_seen_before_ms=100) == D("0.05")
    assert observed.unobserved_cash_claim(cash_seen_before_ms=101) == 0


def test_unknown_population_and_zero_weights_remain_account_unattributed() -> None:
    for kwargs in ({"population_complete": False}, {"activities_complete": False}):
        result = _attribute([_fill("a")], [FeeCharge("c", D("0.05"), 100)], **kwargs)
        assert not result.known and result.unattributed == D("0.05") and not result.shares
    result = _attribute([_fill("a", side=OrderSide.BUY)], [FeeCharge("sec", D("0.05"), 100, component="sec")])
    assert not result.known and result.unattributed == D("0.05")


def test_duplicate_delivery_is_one_charge_and_conflict_fails_closed() -> None:
    charge = FeeCharge("fee", D("0.05"), 100)
    result = _attribute([_fill("a")], [charge, FeeCharge("fee", D("0.05"), 200)])
    assert result.known and result.total_for("a") == D("0.05")
    assert result.shares[0].observed_at_ms == 100
    assert not _attribute([_fill("a")], [charge, FeeCharge("fee", D("0.06"), 200)]).known


def test_proven_order_link_and_explicit_fill_coverage_charge_once() -> None:
    fills = [_fill("a", fee=D("0.05")), _fill("b")]
    result = _attribute(fills, [FeeCharge("fee", D("0.05"), 100, native_order_id="order-a", covers_fill_ids=("a",))])
    assert result.known and result.total_for("a") == D("0.05") and result.total_for("b") == 0
    unknown = _attribute(fills, [FeeCharge("fee", D("0.05"), 100)])
    assert not unknown.known and "overlap" in unknown.unresolved[0]


def test_reported_fill_fee_stays_in_fill_cash_claim() -> None:
    result = _attribute([_fill("a", fee=D("0.05"))])
    assert result.total_for("a") == D("0.05")
    assert result.unobserved_cash_claim(cash_seen_before_ms=0) == 0


def test_linked_refund_reverses_original_shares() -> None:
    result = _attribute(
        [_fill("a"), _fill("b"), _fill("c")],
        [FeeCharge("refund", D("-0.05"), 200, refund_of="original"), FeeCharge("original", D("0.05"), 100)],
    )
    assert result.known and result.observed_total == 0
    assert all(result.total_for(key) == 0 for key in ("a", "b", "c"))
    assert {share.subject_id: share.amount for share in result.shares if share.charge_id == "refund"} == {
        "a": D("-0.02"),
        "b": D("-0.02"),
        "c": D("-0.01"),
    }


def test_unlinked_refund_is_not_guessed_as_new_bot_cash() -> None:
    result = _attribute([_fill("a")], [FeeCharge("refund", D("-0.05"), 200)])
    assert not result.known and not result.shares and result.unattributed == D("-0.05")


def test_simulation_ignores_real_fee_activities_and_reported_real_fee() -> None:
    result = _attribute(
        [_fill("a", fee=D("100"))],
        [FeeCharge("real-fee", D("500"), 100)],
        simulated=True,
        session_ended=True,
        activities_complete=False,
    )
    assert result.known and result.total_for("a") == D("0.03")
    assert {share.state for share in result.shares} == {"modelled_settled"}


@pytest.mark.parametrize(
    "amount,weights",
    [(D("0.001"), {"a": D(1)}), (D("NaN"), {"a": D(1)}), (D("1"), {"a": D(0)}), (D("1"), {"a": D(-1)})],
)
def test_invalid_settlement_never_manufactures_money(amount, weights) -> None:
    with pytest.raises(ValueError):
        apportion_cents(amount, weights)


def test_independent_rational_golden_fixture() -> None:
    import json
    from pathlib import Path

    source = Path(__file__).parents[1] / "fixtures/golden/alpaca-fee-attribution/cases.json"
    for case in json.loads(source.read_text()):
        result = apportion_cents(D(case["cents"]) / 100, {key: D(value) for key, value in case["weights"].items()})
        assert {key: int(value * 100) for key, value in result.items()} == case["expected_cents"]


def test_partial_refunds_cannot_refund_a_cent_twice() -> None:
    result = _attribute([_fill("a"), _fill("b"), _fill("c")], [
        FeeCharge("original", D("0.05"), 100),
        FeeCharge("refund1", D("-0.02"), 200, refund_of="original"),
        FeeCharge("refund2", D("-0.03"), 300, refund_of="original"),
    ])
    assert result.known
    assert all(result.total_for(key) == 0 for key in ("a", "b", "c"))
    excessive = _attribute([_fill("a")], [FeeCharge("original", D("0.01"), 100), FeeCharge("refund1", D("-0.01"), 200, refund_of="original"), FeeCharge("refund2", D("-0.01"), 300, refund_of="original")])
    assert not excessive.known


def test_simulated_settled_fees_already_in_cash_are_not_claimed_twice() -> None:
    result = _attribute([_fill("a")], simulated=True, session_ended=True, settlement_at_ms=100)
    assert result.unobserved_cash_claim(cash_seen_before_ms=1000) == D("0.03")
    assert result.unobserved_cash_claim(cash_seen_before_ms=1000, modelled_fees_seen_before_ms=101) == 0
