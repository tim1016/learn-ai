"""Panel capability profile — the closed per-broker descriptor (spec §4).

Angular renders strictly from this profile: an inapplicable station renders as
``not_applicable``; an unsupported action never renders at all. The profile is
snapshot-contract-tested per broker (``test_panel_profile_snapshot``).

Phase 1 registers only Alpaca. The profile is deliberately a static, closed
descriptor (not probed) — it declares honest vendor differences as data, the
same discipline as ``BrokerCapabilities`` (ADR 0032 principle #2).
"""

from __future__ import annotations

from app.broker.v2panel.vocabulary import ACTION_IDS, STATION_IDS, copy_for
from app.schemas.broker_v2_panel import PanelProfile, StationApplicability

# Broker → the profile facts that differ per broker/mode. A broker absent here
# has no panel profile (the caller raises the read-path 404).
_ALPACA_INAPPLICABLE_STATIONS: frozenset[str] = frozenset()


def _stations_for(inapplicable: frozenset[str]) -> list[StationApplicability]:
    """Build the six station-applicability rows from the closed station set."""
    rows: list[StationApplicability] = []
    for station_id in STATION_IDS:
        copy = copy_for(station_id)
        rows.append(
            StationApplicability(
                station_id=station_id,
                applicable=station_id not in inapplicable,
                label=copy.label,
                explanation=copy.explanation,
            )
        )
    return rows


def alpaca_panel_profile() -> PanelProfile:
    """The closed panel capability profile for Alpaca paper (phase 1).

    - ``fee_fidelity="none"`` — Alpaca's ``trade_updates`` stream reports no
      per-fill commission, so the panel renders "Fees not reported" (§10).
    - ``flatten_supported=True`` — the clerk owns namespaced flatten (§12).
    - ``live_bars_supported=False`` — no Alpaca-native live-bar strain in
      phase 1; the LIVE pane uses the IBKR bridge + Polygon fallback
      (ADR 0032 amendment, §8).
    - All six stations apply; log-only bots simply idle at SIGNAL.
    - ``deploy`` is excluded from the per-bot panel's supported actions here
      because deploy is a global (list-page) action, not a per-bot one — the
      panel-action service pins that split.
    """
    return PanelProfile(
        broker="alpaca",
        fee_fidelity="none",
        flatten_supported=True,
        live_bars_supported=False,
        stations=_stations_for(_ALPACA_INAPPLICABLE_STATIONS),
        supported_action_ids=list(ACTION_IDS),
    )


_PROFILES = {"alpaca": alpaca_panel_profile}


def panel_profile_for(broker: str) -> PanelProfile | None:
    """Return the closed panel profile for ``broker`` or ``None`` if unsupported.

    ``None`` lets the router preserve the read-path 404 semantics (a known
    read-only broker without a panel profile is a 404, not a 500).
    """
    builder = _PROFILES.get(broker)
    return builder() if builder is not None else None
