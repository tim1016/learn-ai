"""Contract tests for the panel capability profile (S1, spec §4).

Snapshot-pins the Alpaca profile per broker: which stations apply, fee
fidelity, live-bar availability.
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
    # No Alpaca-native live-bar strain in phase 1 (ADR 0032 amendment, §8).
    assert profile.live_bars_supported is False


def test_alpaca_profile_covers_all_six_stations() -> None:
    profile = alpaca_panel_profile()
    station_ids = [s.station_id for s in profile.stations]
    assert station_ids == list(STATION_IDS)
    # All six apply for Alpaca; each carries server-authored copy.
    assert all(s.applicable for s in profile.stations)
    assert all(s.label and s.explanation for s in profile.stations)


def test_unknown_broker_has_no_profile() -> None:
    assert panel_profile_for("ibkr") is None
    assert panel_profile_for("nope") is None


def test_alpaca_profile_shape_is_frozen() -> None:
    """The profile's field set is the closed descriptor a future broker matches."""
    profile = alpaca_panel_profile()
    dumped = profile.model_dump()

    # No action list (#2635): a bot's panel presents exactly the actions it
    # may run, so a profile-level list was advertisement nothing read.
    assert set(dumped) == {
        "broker",
        "fee_fidelity",
        "live_bars_supported",
        "stations",
    }
    assert len(dumped["stations"]) == 6
    assert set(dumped["stations"][0]) == {
        "station_id",
        "applicable",
        "label",
        "explanation",
    }
