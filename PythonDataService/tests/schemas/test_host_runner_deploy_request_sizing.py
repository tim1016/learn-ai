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


def test_empty_live_config_rejected() -> None:
    """VCR-0001 / Phase 1 — an empty ``live_config`` would fall through to
    legacy ``SimpleFloorSizing`` (the all-in path that bought $250k of SPY).
    The deploy boundary must refuse it so no new run lands on the legacy
    code path. Sizing-policy-missing is named explicitly in the error."""
    with pytest.raises(ValidationError, match=r"live_config\.sizing is required"):
        HostRunnerDeployRequest(**_base_kwargs(live_config={}))


def test_live_config_without_sizing_key_rejected() -> None:
    """VCR-0001 / Phase 1 — any ``live_config`` that omits ``sizing`` lands on
    the same legacy path as the empty case. Reject it at the schema layer,
    not after the ledger is written."""
    with pytest.raises(ValidationError, match=r"live_config\.sizing is required"):
        HostRunnerDeployRequest(**_base_kwargs(live_config={"symbol": "SPY"}))


def test_unknown_sibling_key_rejected_at_schema_boundary() -> None:
    """VCR-0001 / Phase 1 — unknown ``live_config`` sibling keys must be
    rejected at the schema layer (mirroring ``_live_config_from_ledger``),
    not after ledger creation. Otherwise a stale CLI / typo writes a ledger
    whose ``run_id`` is hashed from a field the runtime will refuse to
    interpret."""
    with pytest.raises(ValidationError, match=r"unknown live_config keys"):
        HostRunnerDeployRequest(
            **_base_kwargs(
                live_config={
                    "future_field": 1,
                    "sizing": {"kind": "FixedShares", "value": 1},
                }
            )
        )


def test_live_config_with_only_known_siblings_accepted() -> None:
    """The fields ``_live_config_from_ledger`` already round-trips (``symbol``,
    ``force_flat_at``, ``consolidator_period_min``, ``max_submit_latency_ms``,
    ``allowed_sessions``) are legal siblings of ``sizing`` and must continue
    to round-trip cleanly."""
    req = HostRunnerDeployRequest(
        **_base_kwargs(
            live_config={
                "symbol": "QQQ",
                "consolidator_period_min": 30,
                "allowed_sessions": ["POST", "RTH"],
                "sizing": {"kind": "FixedShares", "value": 1},
            }
        )
    )
    assert req.live_config == {
        "symbol": "QQQ",
        "consolidator_period_min": 30,
        "allowed_sessions": ["RTH", "POST"],
        "sizing": {"kind": "FixedShares", "value": 1},
    }


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
