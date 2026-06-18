"""Slice 1A — ``live_config.action`` accepted at the deploy boundary.

Mirrors the sizing precedent in
``test_host_runner_deploy_request_sizing.py``: the deploy contract is
intentionally open, but the new ``action`` sub-field must round-trip
through ``HostRunnerDeployRequest`` and be accepted in the
``LIVE_CONFIG_LEDGER_KEYS`` allow-list so it can be hashed into
``run_id`` without being rejected as an unknown sibling.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.engine.live.config import LIVE_CONFIG_LEDGER_KEYS
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


def test_action_key_is_in_ledger_allow_list() -> None:
    """``LIVE_CONFIG_LEDGER_KEYS`` is the master allow-list; adding
    ``action`` must be deliberate and visible. Otherwise the deploy
    validator would silently reject ``live_config.action`` as an
    unknown sibling."""

    assert "action" in LIVE_CONFIG_LEDGER_KEYS


def test_empty_action_plan_round_trips_through_deploy_request() -> None:
    """An empty ``action`` plan alongside the required ``sizing`` is
    accepted at the deploy boundary and persists in the parsed
    ``live_config`` dict."""

    req = HostRunnerDeployRequest(
        **_base_kwargs(
            live_config={
                "sizing": {"kind": "FixedShares", "value": 1},
                "action": {"on_enter": [], "on_exit": []},
            }
        )
    )

    assert req.live_config["action"] == {"on_enter": [], "on_exit": []}
    assert req.live_config["sizing"] == {"kind": "FixedShares", "value": 1}


# ---------------------------------------------------------------------------
# Slice 1B (#595) — stock action plan acceptance + rejection.


_VALID_STOCK_PLAN: dict = {
    "on_enter": [
        {
            "leg_id": "spy_long",
            "instrument": {"kind": "stock", "underlying": "SPY"},
            "position": "long",
            "qty_ratio": 1,
        }
    ],
    "on_exit": [{"kind": "close_leg", "entry_leg_id": "spy_long"}],
}


def test_stock_action_plan_round_trips_through_deploy_request() -> None:
    """A valid stock plan survives Pydantic round-trip through
    ``ActionPlan.model_validate`` and is persisted in the parsed
    ``live_config`` dict ready for ``run_id`` hashing."""

    req = HostRunnerDeployRequest(
        **_base_kwargs(
            live_config={
                "sizing": {"kind": "FixedShares", "value": 1},
                "action": _VALID_STOCK_PLAN,
            }
        )
    )

    assert req.live_config["action"] == _VALID_STOCK_PLAN


def test_malformed_stock_plan_rejected_at_deploy_boundary() -> None:
    """A bad plan (missing ``instrument.underlying``) is rejected by
    Pydantic at the deploy boundary so it never enters the ledger or
    contaminates ``run_id``."""

    bad_plan = {
        "on_enter": [
            {
                "leg_id": "spy_long",
                "instrument": {"kind": "stock"},
                "position": "long",
                "qty_ratio": 1,
            }
        ],
        "on_exit": [],
    }
    with pytest.raises(ValidationError, match=r"underlying"):
        HostRunnerDeployRequest(
            **_base_kwargs(
                live_config={
                    "sizing": {"kind": "FixedShares", "value": 1},
                    "action": bad_plan,
                }
            )
        )
