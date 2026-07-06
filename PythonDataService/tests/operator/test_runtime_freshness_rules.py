from __future__ import annotations

import pytest

from app.operator.notices.runtime_freshness import (
    _RUNTIME_FRESHNESS_RULES,
    compose_runtime_freshness_notices,
)
from app.operator.notices.schema import RuntimeFreshnessReasonCode
from app.services.runtime_freshness import (
    DomainFreshness,
    RuntimeFreshness,
)
from tests.operator._helpers import get_literal_args


def _stale_domain(*codes: str) -> DomainFreshness:
    return DomainFreshness(
        state="STALE" if codes else "FRESH",
        age_ms=99_000 if codes else 0,
        stale_reason_codes=list(codes),
    )


def _fresh_runtime() -> RuntimeFreshness:
    fresh = DomainFreshness(state="FRESH", age_ms=0, stale_reason_codes=[])
    return RuntimeFreshness(
        posture_demoted=False,
        command_loop=fresh,
        broker=fresh,
        bar_loop=fresh,
        control_plane=fresh,
    )


def _runtime_with(*, bar_loop: list[str] | None = None,
                  command_loop: list[str] | None = None,
                  broker: list[str] | None = None,
                  control_plane: list[str] | None = None,
                  posture_demoted: bool = True) -> RuntimeFreshness:
    return RuntimeFreshness(
        posture_demoted=posture_demoted,
        command_loop=_stale_domain(*(command_loop or [])),
        broker=_stale_domain(*(broker or [])),
        bar_loop=_stale_domain(*(bar_loop or [])),
        control_plane=_stale_domain(*(control_plane or [])),
    )


# ---------------------------------------------------------------------------
# Exhaustiveness — the rules table covers every RuntimeFreshnessReasonCode.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", get_literal_args(RuntimeFreshnessReasonCode))
def test_every_reason_code_has_a_rule(code: str) -> None:
    covered: set[str] = set()
    for rule in _RUNTIME_FRESHNESS_RULES:
        covered.update(rule.source_codes)
    assert code in covered, f"reason code {code} is not covered by any rule"


# ---------------------------------------------------------------------------
# Selection — priority resolution, suppress_banner behaviour.
# ---------------------------------------------------------------------------

def test_no_freshness_object_returns_no_headline_or_reasons():
    headline, reasons = compose_runtime_freshness_notices(None)
    assert headline is None
    assert reasons == []


def test_fresh_runtime_returns_no_headline_or_reasons():
    headline, reasons = compose_runtime_freshness_notices(_fresh_runtime())
    assert headline is None
    assert reasons == []


def test_session_closed_is_suppressed_from_headline_but_still_returned():
    runtime = _runtime_with(bar_loop=["BAR_LOOP_SESSION_CLOSED"], posture_demoted=False)
    headline, reasons = compose_runtime_freshness_notices(runtime)
    assert headline is None
    assert [n.code for n in reasons] == ["runtime.market_closed"]


def test_combined_heartbeat_and_latest_bar_emit_feed_stalled():
    runtime = _runtime_with(
        bar_loop=["BAR_LOOP_HEARTBEAT_STALE", "BAR_LOOP_LATEST_BAR_STALE"],
    )
    headline, _reasons = compose_runtime_freshness_notices(runtime)
    assert headline is not None
    assert headline.code == "runtime.market_data_feed_stalled"
    assert headline.tier == "warning"
    assert "BAR_LOOP_HEARTBEAT_STALE" in headline.source_codes
    assert "BAR_LOOP_LATEST_BAR_STALE" in headline.source_codes


def test_missing_first_source_bar_gets_actionable_market_data_notice():
    runtime = _runtime_with(bar_loop=["BAR_LOOP_SOURCE_MISSING"])
    headline, _reasons = compose_runtime_freshness_notices(runtime)

    assert headline is not None
    assert headline.code == "runtime.market_data_stale"
    assert headline.title == "Market data has not started"
    assert headline.action.kind == "external_manual_check"
    assert "BAR_LOOP_SOURCE_MISSING" in headline.source_codes


def test_first_bar_timeout_gets_specific_market_data_notice():
    runtime = _runtime_with(bar_loop=["BAR_LOOP_FIRST_BAR_TIMEOUT"])
    headline, _reasons = compose_runtime_freshness_notices(runtime)

    assert headline is not None
    assert headline.code == "runtime.market_data_first_bar_timeout"
    assert headline.tier == "critical"
    assert headline.title == "IBKR market data is silent"
    assert "competing live session" in headline.message
    assert headline.action.kind == "external_manual_check"
    assert headline.action.label == "Fix IBKR market data"
    assert "BAR_LOOP_FIRST_BAR_TIMEOUT" in headline.source_codes


def test_boot_id_mismatch_wins_over_lease_stale():
    runtime = _runtime_with(
        control_plane=["CONTROL_PLANE_BOOT_ID_MISMATCH", "CONTROL_PLANE_LEASE_STALE"],
    )
    headline, _ = compose_runtime_freshness_notices(runtime)
    assert headline is not None
    assert headline.code == "runtime.control_plane_boot_id_mismatch"
    assert headline.tier == "critical"


def test_command_loop_stale_is_critical():
    runtime = _runtime_with(command_loop=["COMMAND_LOOP_STALE"])
    headline, _ = compose_runtime_freshness_notices(runtime)
    assert headline is not None
    assert headline.code == "runtime.command_loop_unresponsive"
    assert headline.tier == "critical"


def test_broker_probe_missing_outranks_probe_stale_when_both_present():
    runtime = _runtime_with(broker=["BROKER_PROBE_MISSING", "BROKER_PROBE_STALE"])
    headline, additional_reasons = compose_runtime_freshness_notices(runtime)
    assert headline is not None
    assert headline.code == "runtime.broker_probe_missing"
    # broker_probe_missing is the headline and must NOT appear in additional_reasons.
    # broker_probe_stale must appear in additional_reasons (it is a distinct rule).
    codes_in_additional = {n.code for n in additional_reasons}
    assert codes_in_additional == {"runtime.broker_probe_stale"}


def test_session_halted_emits_info_level_notice():
    runtime = _runtime_with(bar_loop=["BAR_LOOP_SESSION_HALTED"], posture_demoted=False)
    headline, _ = compose_runtime_freshness_notices(runtime)
    assert headline is not None
    assert headline.code == "runtime.market_session_halted"
    assert headline.tier == "info"


def test_forensic_facts_include_age_ms_when_available():
    runtime = _runtime_with(bar_loop=["BAR_LOOP_LATEST_BAR_STALE"])
    headline, _ = compose_runtime_freshness_notices(runtime)
    assert headline is not None
    assert headline.forensic_facts.get("bar_loop_age_ms") == 99_000


def test_occurred_at_ms_is_set_when_now_ms_provided():
    runtime = _runtime_with(bar_loop=["BAR_LOOP_LATEST_BAR_STALE"])
    headline, _ = compose_runtime_freshness_notices(runtime, now_ms=1_750_000_000_000)
    assert headline is not None
    assert headline.occurred_at_ms == 1_750_000_000_000


def test_engine_runtime_missing_promotes_to_runtime_incompatible():
    runtime = _runtime_with(command_loop=["ENGINE_RUNTIME_MISSING"])
    headline, _ = compose_runtime_freshness_notices(runtime)
    assert headline is not None
    assert headline.code == "runtime.engine_runtime_incompatible"


def test_reasons_preserve_priority_order_descending():
    runtime = _runtime_with(
        bar_loop=["BAR_LOOP_LATEST_BAR_STALE"],
        broker=["BROKER_PROBE_STALE"],
        control_plane=["CONTROL_PLANE_LEASE_STALE"],
    )
    headline, additional_reasons = compose_runtime_freshness_notices(runtime)
    # headline = control_plane_lease_stale (priority 95, the winner).
    assert headline is not None
    assert headline.code == "runtime.control_plane_lease_stale"
    # additional_reasons must NOT include the headline; order is still priority-descending.
    codes = [n.code for n in additional_reasons]
    assert codes == [
        "runtime.broker_probe_stale",
        "runtime.market_data_stale",
    ]


def test_control_plane_lease_stale_offers_renew_action():
    runtime = _runtime_with(control_plane=["CONTROL_PLANE_LEASE_STALE"])
    headline, _ = compose_runtime_freshness_notices(runtime)
    assert headline is not None
    assert headline.code == "runtime.control_plane_lease_stale"
    assert headline.action.kind == "renew_control_plane_lease"
    assert headline.action.label == "Renew control-plane lease"
    assert headline.action.target == "daemon_lease"


def test_feed_stalled_fires_even_when_other_domain_is_also_stale():
    """Regression for thermo M1: previously ``mode='exact'`` compared against the
    GLOBAL active code set, so a co-occurring stale code in any other domain
    silently knocked out the combined feed_stalled rule and the trader saw two
    redundant ``runtime.market_data_stale`` notices instead of one combined
    ``runtime.market_data_feed_stalled``.
    """
    runtime = _runtime_with(
        bar_loop=["BAR_LOOP_HEARTBEAT_STALE", "BAR_LOOP_LATEST_BAR_STALE"],
        broker=["BROKER_PROBE_STALE"],  # the co-occurring stale code
    )
    headline, reasons = compose_runtime_freshness_notices(runtime)

    # Headline must be the higher-tier broker probe stale, because broker stale
    # ranks above market data stale in the rules table. But the bar-loop
    # combined notice MUST also appear in reasons exactly once — NOT as two
    # separate runtime.market_data_stale entries.
    assert headline is not None
    market_data_codes = [n.code for n in reasons if n.code.startswith("runtime.market_data")]
    assert market_data_codes == ["runtime.market_data_feed_stalled"], (
        f"expected single feed_stalled notice; got {market_data_codes}"
    )
