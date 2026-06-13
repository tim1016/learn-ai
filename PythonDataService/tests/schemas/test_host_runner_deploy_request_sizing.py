"""ADR 0009 — ``live_config.sizing`` validation at the deploy API boundary.

The deploy contract surface is intentionally open (``live_config: dict``);
the ``sizing`` sub-field is the only one ADR 0009 locks down here. Catching
a malformed policy at this boundary stops a bad form from being hashed into
``run_id`` downstream.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.live_runs import HostRunnerDeployRequest


def _base_kwargs(**overrides: object) -> dict:
    defaults = dict(
        strategy_spec_path="a/b.json",
        qc_audit_copy_path="c/d.py",
        qc_cloud_backtest_id="bt-1",
        account_id="DU111",
        start_date_ms=0,
    )
    defaults.update(overrides)
    return defaults


def test_safe_canary_round_trips_through_schema() -> None:
    req = HostRunnerDeployRequest(
        **_base_kwargs(live_config={"sizing": {"kind": "FixedShares", "value": 1}})
    )
    assert req.live_config == {"sizing": {"kind": "FixedShares", "value": 1}}


def test_set_holdings_fraction_canonicalized_to_decimal_string() -> None:
    """Wire form: a SetHoldings fraction passed as decimal string round-trips
    as the same canonical decimal string. Hash stability requires this — the
    operator never sees a float on the wire."""
    req = HostRunnerDeployRequest(
        **_base_kwargs(live_config={"sizing": {"kind": "SetHoldings", "fraction": "1.0"}})
    )
    assert req.live_config == {"sizing": {"kind": "SetHoldings", "fraction": "1.0"}}


def test_absent_sizing_is_passthrough() -> None:
    """live_config without a sizing key is unmodified — legacy/unknown."""
    req = HostRunnerDeployRequest(**_base_kwargs(live_config={"symbol": "SPY"}))
    assert req.live_config == {"symbol": "SPY"}


def test_malformed_sizing_kind_rejected() -> None:
    with pytest.raises(ValidationError, match=r"invalid live_config\.sizing"):
        HostRunnerDeployRequest(
            **_base_kwargs(live_config={"sizing": {"kind": "Bogus", "value": 1}})
        )


def test_fixed_shares_below_one_rejected() -> None:
    with pytest.raises(ValidationError, match=r"invalid live_config\.sizing"):
        HostRunnerDeployRequest(
            **_base_kwargs(live_config={"sizing": {"kind": "FixedShares", "value": 0}})
        )


def test_fixed_notional_raw_float_rejected() -> None:
    with pytest.raises(ValidationError, match=r"invalid live_config\.sizing"):
        HostRunnerDeployRequest(
            **_base_kwargs(live_config={"sizing": {"kind": "FixedNotional", "value": 1000.5}})
        )


def test_strategy_explicit_round_trips() -> None:
    req = HostRunnerDeployRequest(
        **_base_kwargs(live_config={"sizing": {"kind": "StrategyExplicit"}})
    )
    assert req.live_config == {"sizing": {"kind": "StrategyExplicit"}}
