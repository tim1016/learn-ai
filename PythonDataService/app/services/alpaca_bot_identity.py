"""Positive Alpaca identity guard for SQLite-owned lifecycle mutations."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from app.services.bot_binding_repository import live_state_binding_repository


class AlpacaBotIdentityRefusedError(RuntimeError):
    """Durable evidence did not positively identify an Alpaca bot."""


class AlpacaBotIdentityGuard:
    """Require SQLite authority and a non-conflicting readable binding."""

    def __init__(self, artifacts_root: Path) -> None:
        self._bindings = live_state_binding_repository(Path(artifacts_root))

    def require(self, strategy_instance_id: str, *, sqlite_claim: bool) -> None:
        try:
            binding = self._bindings.read(strategy_instance_id)
        except (OSError, ValidationError, ValueError) as exc:
            raise AlpacaBotIdentityRefusedError(
                f"{strategy_instance_id!r} has an unreadable broker binding"
            ) from exc
        if not sqlite_claim:
            raise AlpacaBotIdentityRefusedError(
                f"{strategy_instance_id!r} has no active SQLite Alpaca authority"
            )
        if binding is not None and binding.broker != "alpaca":
            raise AlpacaBotIdentityRefusedError(
                f"{strategy_instance_id!r} has non-Alpaca broker identity {binding.broker!r}"
            )


__all__ = ["AlpacaBotIdentityGuard", "AlpacaBotIdentityRefusedError"]
