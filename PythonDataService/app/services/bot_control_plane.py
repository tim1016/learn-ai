"""Positive broker-plane identity for transitional duty mutations."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from app.engine.live.identity import strategy_instance_artifact_dir
from app.engine.live.run_ledger import LiveRunLedger
from app.services.bot_binding_repository import BotBindingRepository


class BotControlPlane(StrEnum):
    ALPACA_RUNNER = "ALPACA_RUNNER"
    IBKR_EVALUATOR = "IBKR_EVALUATOR"


class ControlPlaneRefusedError(RuntimeError):
    """Durable evidence did not positively identify the expected plane."""


class BotControlPlaneDiscriminator:
    """Resolve one immutable bot identity without inferring from absence."""

    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = Path(artifacts_root)
        self._bindings = BotBindingRepository(
            self._artifacts_root,
            instance_dir_for=lambda strategy_instance_id: strategy_instance_artifact_dir(
                self._artifacts_root,
                "live_state",
                strategy_instance_id,
            ),
        )

    def require(
        self,
        strategy_instance_id: str,
        expected: BotControlPlane,
        *,
        sqlite_alpaca_claim: bool = False,
    ) -> None:
        binding_plane = self._binding_plane(strategy_instance_id)
        legacy_claim = self._has_legacy_ledger(strategy_instance_id)
        alpaca_claim = (
            binding_plane is BotControlPlane.ALPACA_RUNNER or sqlite_alpaca_claim
        )
        ibkr_claim = binding_plane is BotControlPlane.IBKR_EVALUATOR or legacy_claim
        if alpaca_claim and ibkr_claim:
            raise ControlPlaneRefusedError(
                f"{strategy_instance_id!r} has ambiguous Alpaca and IBKR identity evidence"
            )
        actual = (
            BotControlPlane.ALPACA_RUNNER
            if alpaca_claim
            else (BotControlPlane.IBKR_EVALUATOR if ibkr_claim else None)
        )
        if actual is None:
            raise ControlPlaneRefusedError(
                f"{strategy_instance_id!r} has no readable broker-plane identity"
            )
        if actual is not expected:
            raise ControlPlaneRefusedError(
                f"{strategy_instance_id!r} belongs to {actual.value}, not {expected.value}"
            )

    def _binding_plane(self, strategy_instance_id: str) -> BotControlPlane | None:
        try:
            binding = self._bindings.read(strategy_instance_id)
        except (OSError, ValidationError, ValueError) as exc:
            raise ControlPlaneRefusedError(
                f"{strategy_instance_id!r} has an unreadable broker binding"
            ) from exc
        if binding is None:
            return None
        if binding.broker == "alpaca":
            return BotControlPlane.ALPACA_RUNNER
        if binding.broker == "ibkr":
            return BotControlPlane.IBKR_EVALUATOR
        raise ControlPlaneRefusedError(
            f"{strategy_instance_id!r} has unsupported broker identity {binding.broker!r}"
        )

    def _has_legacy_ledger(self, strategy_instance_id: str) -> bool:
        live_runs = self._artifacts_root / "live_runs"
        if not live_runs.is_dir():
            return False
        found = False
        for ledger_path in live_runs.glob("*/run_ledger.json"):
            try:
                raw = json.loads(ledger_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict) or raw.get("strategy_instance_id") != strategy_instance_id:
                continue
            try:
                LiveRunLedger.model_validate(raw)
            except ValidationError as exc:
                raise ControlPlaneRefusedError(
                    f"{strategy_instance_id!r} has an unreadable IBKR run binding"
                ) from exc
            found = True
        return found


__all__ = [
    "BotControlPlane",
    "BotControlPlaneDiscriminator",
    "ControlPlaneRefusedError",
]
