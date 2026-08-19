from __future__ import annotations

from app.schemas.broker_session import GatewaySocketRow
from app.schemas.live_runs import AccountClerkHealth
from app.services.broker_session_reconciler import reconcile_broker_session_snapshot


def test_unknown_socket_is_evidence_without_legacy_bot_attribution() -> None:
    result = reconcile_broker_session_snapshot(
        socket_rows=[GatewaySocketRow(pid=42, command="python", remote_port=4002)],
        data_plane_health=None,
        as_of_ms=1_780_000_000_000,
    )

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.identity_type == "ghost"
    assert row.strategy_instance_id is None
    assert row.run_id is None
    assert row.registry_claim is None
    assert row.attention_codes == ["GHOST_SOCKET"]


def test_current_account_clerk_socket_is_classified_as_infrastructure() -> None:
    result = reconcile_broker_session_snapshot(
        socket_rows=[
            GatewaySocketRow(
                pid=84,
                command="python",
                argv=[
                    "python",
                    "-m",
                    "app.engine.live.account_clerk",
                    "--account-id",
                    "DU123",
                    "--generation",
                    "3",
                ],
                remote_port=4002,
            )
        ],
        data_plane_health=None,
        account_clerks=[
            AccountClerkHealth(
                account_id="DU123",
                generation=3,
                pid=84,
                status="RUNNING",
                started_at_ms=1,
                lease_valid=True,
            )
        ],
        as_of_ms=1_780_000_000_000,
    )

    assert result.rows == []
    assert [event.code for event in result.global_events] == [
        "ACCOUNT_CLERK_BROKER_CLIENT"
    ]
