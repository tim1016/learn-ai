from __future__ import annotations

from app.operator.notices.schema import OperatorNotice, OperatorNoticeAction


def orphaned_socket_notice(
    *,
    strategy_instance_id: str,
    run_id: str | None,
    client_id: int | None,
    local_port: int | None,
    remote_port: int | None,
    run_dir: str | None,
    observed_at_ms: int,
) -> OperatorNotice:
    """ADR 0018 notice for a bot-attributed socket with no live child PID."""
    return OperatorNotice(
        code="broker_session.orphaned_socket",
        tier="critical",
        title="Orphaned broker socket detected",
        message=(
            f"IB Gateway still shows a broker socket for {strategy_instance_id}, "
            "but the host process is not live. Verify the client session in IBKR "
            "and reconcile broker orders and positions before restarting this bot."
        ),
        source_codes=["SOCKET_WITHOUT_LIVE_PID", "ORPHANED_BOT_SOCKET"],
        forensic_facts={
            "strategy_instance_id": strategy_instance_id,
            "run_id": run_id,
            "client_id": client_id,
            "local_port": local_port,
            "remote_port": remote_port,
            "run_dir": run_dir,
            "observed_at_ms": observed_at_ms,
        },
        action=OperatorNoticeAction(
            kind="focus_cockpit_action",
            label="Open Bot Cockpit",
            target=strategy_instance_id,
        ),
        runbook_slug="broker-session-orphaned-socket",
        occurred_at_ms=observed_at_ms,
    )
