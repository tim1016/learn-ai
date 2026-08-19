"""Positive Alpaca identity guard for SQLite-owned lifecycle mutations."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from app.engine.live.identity import strategy_instance_artifact_dir
from app.services.bot_binding_repository import BotBindingRepository


class AlpacaBotIdentityRefusedError(RuntimeError):
    """Durable evidence did not positively identify an Alpaca bot."""


class AlpacaBotIdentityGuard:
    """Require SQLite authority and a non-conflicting readable binding."""

    def __init__(self, artifacts_root: Path) -> None:
        root = Path(artifacts_root)
        self._bindings = BotBindingRepository(
            root,
            instance_dir_for=lambda strategy_instance_id: strategy_instance_artifact_dir(
                root,
                "live_state",
                strategy_instance_id,
            ),
        )

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
