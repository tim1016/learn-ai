"""The owner's copy for a lane with no account authority is the truth (#2582).

2026-09-29: the Live lane's startup could not reach Alpaca for a few seconds,
and every routed account read answered "failed serving account_money_read
with 503. Retry once the lane recovers from the reported server error; the
lane's own log names the cause." Retrying could not help -- nothing retried
the selection -- and the log did not name the cause.

The lane's beat reports its account authority on every pass, and the
coordinator authors a routed 5xx's copy from the session it routed through:
reconnecting recovers on its own; a bound lane whose authority is
unavailable needs a restart and never promises a retry. One channel carries
the fact -- the beat -- and nothing rides the identity echo.
"""

from __future__ import annotations

from pathlib import Path

from app.broker.alpaca.clerk.fleet_boot import FleetLaneBoot, heartbeat_facts, report_authority_state
from app.broker.fleet.presence import SessionInfo


def _lane_boot(tmp_path: Path) -> FleetLaneBoot:
    """A registered lane; its presence client is never called by what these tests read."""
    return FleetLaneBoot(
        presence=None,  # type: ignore[arg-type]
        clerk_id="clrk_live",
        worker_key="alpaca-live-clerk",
        volume_root=tmp_path,
        registry_id="registry",
        volume_id="volume",
        session=SessionInfo(agent_instance_id="agent-1", routing_epoch=3),
    )


def test_the_beat_reports_reconnecting_then_what_the_reconnect_ended_in(tmp_path: Path) -> None:
    """The directory tells the same truth: reconnecting while it retries, then what installed."""
    boot = _lane_boot(tmp_path)
    boot.reported_facts = heartbeat_facts(
        account_pin="9LIVE0001",
        effective_binding_generation=4,
        authority_kind="unavailable",
        endpoint_mode="live",
        reconnecting=True,
    )
    assert boot.reported_facts["reported_summary"] == {
        "endpoint_mode": "live",
        "authority_state": "reconnecting",
    }

    # A retired attempt goes back to reconnecting; the beat says so, not "unavailable".
    report_authority_state(boot, authority_kind="unavailable", reconnecting=True)
    assert boot.reported_facts["reported_summary"]["authority_state"] == "reconnecting"

    report_authority_state(boot, authority_kind="sqlite")

    assert boot.reported_facts["reported_summary"] == {
        "endpoint_mode": "live",
        "authority_state": "real_live",
    }
    # The grant it confirmed at boot is what it still reports; nothing is re-confirmed.
    assert boot.reported_facts["reported_state"] == "binding_confirmed"
    assert boot.reported_facts["reported_binding_generation"] == 4


def test_a_reconnect_that_ends_final_is_reported_unavailable(tmp_path: Path) -> None:
    boot = _lane_boot(tmp_path)
    boot.reported_facts = heartbeat_facts(
        account_pin="9LIVE0001",
        effective_binding_generation=4,
        authority_kind="unavailable",
        endpoint_mode="live",
        reconnecting=True,
    )

    report_authority_state(boot, authority_kind="unavailable", reconnecting=False)

    assert boot.reported_facts["reported_summary"]["authority_state"] == "unavailable"
