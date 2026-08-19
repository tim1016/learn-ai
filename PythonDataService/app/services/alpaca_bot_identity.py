"""Positive Alpaca identity guard for SQLite-owned lifecycle mutations."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from app.engine.live.identity import strategy_instance_artifact_dir
from app.engine.live.run_ledger import read_ledger
from app.services.bot_binding_repository import BotBindingRepository


class AlpacaBotIdentityRefusedError(RuntimeError):
    """Durable evidence did not positively identify an Alpaca bot."""


def _legacy_ibkr_run_dir(
    artifacts_root: Path,
    strategy_instance_id: str,
) -> Path | None:
    """Return matching retired IBKR identity, refusing ambiguous ledgers."""

    live_runs_root = artifacts_root / "live_runs"
    if not live_runs_root.exists():
        return None
    if live_runs_root.is_symlink() or not live_runs_root.is_dir():
        raise OSError("legacy live_runs root is not a readable directory")
    for run_dir in live_runs_root.iterdir():
        if run_dir.is_symlink() or not run_dir.is_dir():
            continue
        ledger_path = run_dir / "run_ledger.json"
        if not ledger_path.exists():
            continue
        if ledger_path.is_symlink():
            raise OSError("legacy run ledger must not be a symbolic link")
        ledger = read_ledger(ledger_path)
        if ledger.strategy_instance_id == strategy_instance_id:
            return run_dir
    return None


class AlpacaBotIdentityGuard:
    """Require SQLite authority and a non-conflicting readable binding."""

    def __init__(self, artifacts_root: Path) -> None:
        root = Path(artifacts_root)
        self._artifacts_root = root
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
        try:
            legacy_run_dir = _legacy_ibkr_run_dir(
                self._artifacts_root,
                strategy_instance_id,
            )
        except (OSError, ValidationError, ValueError) as exc:
            raise AlpacaBotIdentityRefusedError(
                f"{strategy_instance_id!r} has unreadable historical run identity"
            ) from exc
        if legacy_run_dir is not None:
            raise AlpacaBotIdentityRefusedError(
                f"{strategy_instance_id!r} has historical IBKR run identity"
            )


__all__ = ["AlpacaBotIdentityGuard", "AlpacaBotIdentityRefusedError"]
