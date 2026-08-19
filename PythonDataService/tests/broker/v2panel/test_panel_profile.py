"""Contract tests for the panel capability profile (S1, spec §4).

Snapshot-pins the Alpaca profile per broker: which stations apply, flatten
support, fee fidelity, live-bar availability, supported action ids.
"""

from __future__ import annotations

from app.broker.v2panel.vocabulary import STATION_IDS
from app.services.broker_v2_panel.panel_profile_service import (
    alpaca_panel_profile,
    panel_profile_for,
)


def test_alpaca_profile_is_the_closed_descriptor() -> None:
    profile = alpaca_panel_profile()

    assert profile.broker == "alpaca"
    # Alpaca's trade_updates stream reports no per-fill commission (§10).
    assert profile.fee_fidelity == "none"
    # The clerk owns namespaced flatten (§12).
    assert profile.flatten_supported is True
    # No Alpaca-native live-bar strain in phase 1 (ADR 0032 amendment, §8).
    assert profile.live_bars_supported is False


def test_alpaca_profile_covers_all_six_stations() -> None:
    profile = alpaca_panel_profile()
    station_ids = [s.station_id for s in profile.stations]
    assert station_ids == list(STATION_IDS)
    # All six apply for Alpaca; each carries server-authored copy.
    assert all(s.applicable for s in profile.stations)
    assert all(s.label and s.explanation for s in profile.stations)


def test_alpaca_profile_advertises_only_actions_with_production_performers() -> None:
    profile = alpaca_panel_profile()
    assert profile.supported_action_ids == [
        "deploy",
        "resume",
        "pause",
        "continue",
        "stop",
        "flatten_stop",
        "reconcile_now",
    ]
    assert "retire" not in profile.supported_action_ids
    assert "cancel_order" not in profile.supported_action_ids


def test_unknown_broker_has_no_profile() -> None:
    assert panel_profile_for("ibkr") is None
    assert panel_profile_for("nope") is None


def test_alpaca_profile_shape_is_frozen() -> None:
    """The profile's field set is the closed descriptor a future broker matches."""
    profile = alpaca_panel_profile()
    dumped = profile.model_dump()

    assert set(dumped) == {
        "broker",
        "fee_fidelity",
        "flatten_supported",
        "live_bars_supported",
        "stations",
        "supported_action_ids",
    }
    assert len(dumped["stations"]) == 6
    assert set(dumped["stations"][0]) == {
        "station_id",
        "applicable",
        "label",
        "explanation",
    }
