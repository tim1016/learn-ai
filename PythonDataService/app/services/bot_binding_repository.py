"""Immutable strategy-instance and append-only bot-run repository.

The in-container runner owns task liveness, while this module owns the
versioned records used to restore immutable deployment configuration and the
current run. Reads may lift the Alpaca v1 shape in memory, but never rewrite
historical artifacts, which preserves their byte-level audit evidence.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.engine.live.durable_append_log import (
    create_atomic_exclusive_durable_file,
    create_exclusive_durable_file,
)
from app.engine.live.run_status import _atomic_write_json
from app.schemas.action_plan import ActionPlan, CloseLegExit, StockEntryLeg, StockInstrument
from app.schemas.bot_lifecycle import BotDutyOutcomeKind
from app.schemas.bot_run_evidence import BotCrashDiagnostic
from app.services.bot_carryover import configuration_hash

logger = logging.getLogger(__name__)

BINDING_FILENAME = "broker_binding.json"
STRATEGY_INSTANCE_FILENAME = "strategy_instance.json"
CURRENT_RUN_FILENAME = "current_run.json"
RUNS_DIRECTORY = "runs"
RUN_OUTCOMES_DIRECTORY = "run_outcomes"
_RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class StrategyInstanceConfigurationConflictError(ValueError):
    """One immutable instance identity was reused with different semantics."""


class RunIdentityConflictError(ValueError):
    """One run identity was reused with a different durable payload."""


class RunOutcomeConflictError(ValueError):
    """One run received conflicting terminal evidence."""


def alpaca_v1_action_plan(symbol: str) -> ActionPlan:
    """Build the v1 stock plan from the existing deploy controls."""
    return ActionPlan(
        on_enter=[
            StockEntryLeg(
                leg_id="primary",
                instrument=StockInstrument(kind="stock", underlying=symbol),
                position="long",
                qty_ratio=1,
            )
        ],
        on_exit=[CloseLegExit(kind="close_leg", entry_leg_id="primary")],
    )


class BrokerBotBinding(BaseModel):
    """Runner-facing aggregate of immutable instance and current run records."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[2] = 2
    strategy_instance_id: str
    # Version-1 Alpaca bindings predated this field and all used the only
    # available deployment strategy. The default lifts those durable records.
    strategy_key: str = "deployment_validation"
    broker: str
    symbol: str
    use_rth: bool = True
    mode: Literal["log_only", "dry_run", "trade"] = "log_only"
    quantity: int = 1
    carryover_policy: Literal["FORBID", "ALLOW"] = "FORBID"
    action_plan: ActionPlan
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    created_at_ms: int


class StrategyInstanceRecord(BaseModel):
    """Create-once strategy configuration shared by every run of an instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    strategy_instance_id: str
    strategy_key: str
    broker: str
    symbol: str
    use_rth: bool
    mode: Literal["log_only", "dry_run", "trade"]
    quantity: int
    carryover_policy: Literal["FORBID", "ALLOW"]
    action_plan: ActionPlan
    configuration_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at_ms: int

    @classmethod
    def from_binding(cls, binding: BrokerBotBinding) -> StrategyInstanceRecord:
        return cls(
            strategy_instance_id=binding.strategy_instance_id,
            strategy_key=binding.strategy_key,
            broker=binding.broker,
            symbol=binding.symbol,
            use_rth=binding.use_rth,
            mode=binding.mode,
            quantity=binding.quantity,
            carryover_policy=binding.carryover_policy,
            action_plan=binding.action_plan,
            configuration_hash=configuration_hash(binding),
            created_at_ms=binding.created_at_ms,
        )


class BotRunRecord(BaseModel):
    """Create-once evidence that one run identity was launched."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    strategy_instance_id: str
    configuration_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    launch_reason: Literal["deploy", "resume", "legacy"]
    started_at_ms: int


class CurrentRunBinding(BaseModel):
    """Replaceable pointer to the newest run bound to one instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    strategy_instance_id: str
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    bound_at_ms: int


class BotRunOutcomeRecord(BaseModel):
    """Create-once process-owner terminal evidence for one run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    strategy_instance_id: str
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    kind: BotDutyOutcomeKind
    reason_code: str = Field(min_length=1, max_length=128)
    recorded_at_ms: int = Field(ge=0)
    crash_diagnostic: BotCrashDiagnostic | None = None

    @model_validator(mode="after")
    def require_crashed_outcome_for_diagnostic(self) -> BotRunOutcomeRecord:
        if self.crash_diagnostic is not None and self.kind != "CRASHED":
            raise ValueError("crash diagnostics require a CRASHED terminal outcome")
        return self


class BotBindingRepository:
    """Persist immutable strategy instances and append-only run identities."""

    def __init__(
        self,
        artifacts_root: Path,
        *,
        instance_dir_for: Callable[[str], Path],
    ) -> None:
        self._live_state_root = Path(artifacts_root) / "live_state"
        self._instance_dir_for = instance_dir_for

    def record_launch(
        self,
        binding: BrokerBotBinding,
        *,
        launch_reason: Literal["deploy", "resume"],
    ) -> None:
        """Durably append one run, then make it the instance's current run."""
        instance_dir = self._instance_dir_for(binding.strategy_instance_id)
        instance_dir.mkdir(parents=True, exist_ok=True)
        self._live_state_root.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_binding(instance_dir)

        instance = StrategyInstanceRecord.from_binding(binding)
        self._ensure_instance(instance_dir, instance)
        run = self._run_from_binding(binding, launch_reason=launch_reason)
        self._ensure_run(instance_dir, run)
        _atomic_write_json(
            instance_dir / CURRENT_RUN_FILENAME,
            CurrentRunBinding(
                strategy_instance_id=binding.strategy_instance_id,
                run_id=binding.run_id,
                bound_at_ms=binding.created_at_ms,
            ).model_dump(mode="json"),
        )

    def read(self, strategy_instance_id: str) -> BrokerBotBinding | None:
        """Return the current runner aggregate, with read-only legacy lifting."""
        instance_dir = self._instance_dir_for(strategy_instance_id)
        instance_path = instance_dir / STRATEGY_INSTANCE_FILENAME
        if instance_path.is_file():
            normalized = self._read_normalized(instance_dir)
            if normalized is not None:
                return normalized
        return self._read_legacy_binding(instance_dir)

    def list_for_broker(self, broker: str) -> list[BrokerBotBinding]:
        """Return all valid bindings for a broker without hiding corrupt rows."""
        if not self._live_state_root.is_dir():
            return []

        bindings: list[BrokerBotBinding] = []
        for child in sorted(self._live_state_root.iterdir()):
            if not child.is_dir() or not (
                (child / STRATEGY_INSTANCE_FILENAME).is_file() or (child / BINDING_FILENAME).is_file()
            ):
                continue
            try:
                binding = self.read(child.name)
            except (OSError, ValidationError, ValueError) as exc:
                logger.warning(
                    "Skipping corrupt broker binding",
                    extra={
                        "action": "corrupt_broker_binding_skipped",
                        "path": str(child),
                        "error": str(exc),
                    },
                )
                continue
            if binding is not None and binding.broker == broker:
                bindings.append(binding)
        return bindings

    def list_runs(self, strategy_instance_id: str) -> list[BotRunRecord]:
        """Return newest-first append-only launch evidence for one instance."""
        instance_dir = self._instance_dir_for(strategy_instance_id)
        instance_path = instance_dir / STRATEGY_INSTANCE_FILENAME
        if not instance_path.is_file():
            legacy = self._read_legacy_binding(instance_dir)
            return (
                [self._run_from_binding(legacy, launch_reason="legacy")]
                if legacy is not None
                else []
            )
        instance = StrategyInstanceRecord.model_validate_json(instance_path.read_text(encoding="utf-8"))
        if instance.strategy_instance_id != strategy_instance_id:
            raise ValueError("strategy-instance evidence belongs to another identity")
        runs_dir = instance_dir / RUNS_DIRECTORY
        if not runs_dir.is_dir():
            return []
        runs = [BotRunRecord.model_validate_json(path.read_text(encoding="utf-8")) for path in runs_dir.glob("*.json")]
        for run in runs:
            if (
                run.strategy_instance_id != instance.strategy_instance_id
                or run.configuration_hash != instance.configuration_hash
            ):
                raise ValueError("run history evidence does not match its strategy instance")
        return sorted(
            runs,
            key=lambda run: (run.started_at_ms, run.run_id),
            reverse=True,
        )

    def read_run(
        self,
        strategy_instance_id: str,
        run_id: str,
    ) -> BotRunRecord | None:
        """Read one run directly; current-run polling never scans history."""
        instance_dir = self._instance_dir_for(strategy_instance_id)
        path = self._run_path(instance_dir, run_id)
        if not path.is_file():
            legacy = self._read_legacy_binding(instance_dir)
            return (
                self._run_from_binding(legacy, launch_reason="legacy")
                if legacy is not None and legacy.run_id == run_id
                else None
            )
        run = BotRunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        if run.strategy_instance_id != strategy_instance_id or run.run_id != run_id:
            raise ValueError("run history evidence belongs to another identity")
        instance_path = instance_dir / STRATEGY_INSTANCE_FILENAME
        if instance_path.is_file():
            instance = StrategyInstanceRecord.model_validate_json(
                instance_path.read_text(encoding="utf-8")
            )
            if (
                instance.strategy_instance_id != strategy_instance_id
                or run.configuration_hash != instance.configuration_hash
            ):
                raise ValueError("run history evidence does not match its strategy instance")
        return run

    def record_outcome(self, candidate: BotRunOutcomeRecord) -> None:
        """Durably attach one terminal receipt to an existing run."""
        instance_dir = self._instance_dir_for(candidate.strategy_instance_id)
        run = BotRunRecord.model_validate_json(
            self._run_path(instance_dir, candidate.run_id).read_text(encoding="utf-8")
        )
        if (
            run.strategy_instance_id != candidate.strategy_instance_id
            or run.run_id != candidate.run_id
        ):
            raise ValueError("terminal outcome belongs to another strategy instance")
        outcomes_dir = instance_dir / RUN_OUTCOMES_DIRECTORY
        outcomes_dir.mkdir(parents=True, exist_ok=True)
        path = outcomes_dir / f"{candidate.run_id}.json"
        try:
            self._create_atomic_once(path, candidate)
            return
        except FileExistsError:
            existing = BotRunOutcomeRecord.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        same_terminal_fact = (
            existing.strategy_instance_id == candidate.strategy_instance_id
            and existing.run_id == candidate.run_id
            and existing.kind == candidate.kind
            and existing.reason_code == candidate.reason_code
            and existing.crash_diagnostic == candidate.crash_diagnostic
        )
        if not same_terminal_fact:
            raise RunOutcomeConflictError(
                f"Run '{candidate.run_id}' already has different terminal evidence."
            )

    def read_outcome(
        self,
        strategy_instance_id: str,
        run_id: str,
    ) -> BotRunOutcomeRecord | None:
        """Return proven terminal evidence for one run when it exists."""
        instance_dir = self._instance_dir_for(strategy_instance_id)
        path = (
            instance_dir
            / RUN_OUTCOMES_DIRECTORY
            / f"{self._validate_run_id(run_id)}.json"
        )
        if not path.is_file():
            return None
        outcome = BotRunOutcomeRecord.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        if (
            outcome.strategy_instance_id != strategy_instance_id
            or outcome.run_id != run_id
        ):
            raise ValueError("terminal outcome belongs to another run identity")
        return outcome

    def _migrate_legacy_binding(self, instance_dir: Path) -> None:
        """Replay-safe normalization when a later launch needs legacy history."""
        legacy = self._read_legacy_binding(instance_dir)
        if legacy is None:
            return
        instance = StrategyInstanceRecord.from_binding(legacy)
        self._ensure_instance(instance_dir, instance)
        self._ensure_run(
            instance_dir,
            self._run_from_binding(legacy, launch_reason="legacy"),
        )
        current_path = instance_dir / CURRENT_RUN_FILENAME
        if not current_path.exists():
            _atomic_write_json(
                current_path,
                CurrentRunBinding(
                    strategy_instance_id=legacy.strategy_instance_id,
                    run_id=legacy.run_id,
                    bound_at_ms=legacy.created_at_ms,
                ).model_dump(mode="json"),
            )

    def _read_legacy_binding(self, instance_dir: Path) -> BrokerBotBinding | None:
        legacy_path = instance_dir / BINDING_FILENAME
        if not legacy_path.is_file():
            return None
        return self.decode(legacy_path.read_text(encoding="utf-8"))

    @staticmethod
    def _run_from_binding(
        binding: BrokerBotBinding,
        *,
        launch_reason: Literal["deploy", "resume", "legacy"],
    ) -> BotRunRecord:
        return BotRunRecord(
            run_id=binding.run_id,
            strategy_instance_id=binding.strategy_instance_id,
            configuration_hash=configuration_hash(binding),
            launch_reason=launch_reason,
            started_at_ms=binding.created_at_ms,
        )

    def _ensure_instance(
        self,
        instance_dir: Path,
        candidate: StrategyInstanceRecord,
    ) -> None:
        path = instance_dir / STRATEGY_INSTANCE_FILENAME
        try:
            self._create_once(path, candidate)
            return
        except FileExistsError:
            existing = StrategyInstanceRecord.model_validate_json(path.read_text(encoding="utf-8"))
        immutable_existing = existing.model_dump(exclude={"created_at_ms"})
        immutable_candidate = candidate.model_dump(exclude={"created_at_ms"})
        if immutable_existing != immutable_candidate:
            raise StrategyInstanceConfigurationConflictError(
                f"Strategy instance '{candidate.strategy_instance_id}' already has different immutable configuration."
            )

    def _ensure_run(self, instance_dir: Path, candidate: BotRunRecord) -> None:
        runs_dir = instance_dir / RUNS_DIRECTORY
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = self._run_path(instance_dir, candidate.run_id)
        try:
            self._create_once(path, candidate)
            return
        except FileExistsError:
            existing = BotRunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        if existing != candidate:
            raise RunIdentityConflictError(f"Run '{candidate.run_id}' already has different durable launch evidence.")

    @classmethod
    def _run_path(cls, instance_dir: Path, run_id: str) -> Path:
        return instance_dir / RUNS_DIRECTORY / f"{cls._validate_run_id(run_id)}.json"

    @staticmethod
    def _validate_run_id(run_id: str) -> str:
        if re.fullmatch(_RUN_ID_PATTERN, run_id) is None:
            raise ValueError("run_id is not a valid artifact identity")
        return run_id

    def _create_once(self, path: Path, record: BaseModel) -> None:
        serialized = json.dumps(
            record.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        create_exclusive_durable_file(
            path,
            serialized,
            trusted_root=self._live_state_root,
        )

    def _create_atomic_once(self, path: Path, record: BaseModel) -> None:
        serialized = json.dumps(
            record.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        create_atomic_exclusive_durable_file(
            path,
            serialized,
            trusted_root=self._live_state_root,
        )

    @staticmethod
    def _read_normalized(instance_dir: Path) -> BrokerBotBinding | None:
        instance = StrategyInstanceRecord.model_validate_json(
            (instance_dir / STRATEGY_INSTANCE_FILENAME).read_text(encoding="utf-8")
        )
        current_path = instance_dir / CURRENT_RUN_FILENAME
        if not current_path.exists():
            return None
        current = CurrentRunBinding.model_validate_json(
            current_path.read_text(encoding="utf-8")
        )
        if current.strategy_instance_id != instance.strategy_instance_id:
            raise ValueError("current run pointer belongs to another strategy instance")
        run_path = instance_dir / RUNS_DIRECTORY / f"{current.run_id}.json"
        if not run_path.exists():
            return None
        run = BotRunRecord.model_validate_json(
            run_path.read_text(encoding="utf-8")
        )
        if (
            run.strategy_instance_id != instance.strategy_instance_id
            or run.configuration_hash != instance.configuration_hash
        ):
            raise ValueError("current run evidence does not match its strategy instance")
        binding = BrokerBotBinding(
            strategy_instance_id=instance.strategy_instance_id,
            strategy_key=instance.strategy_key,
            broker=instance.broker,
            symbol=instance.symbol,
            use_rth=instance.use_rth,
            mode=instance.mode,
            quantity=instance.quantity,
            carryover_policy=instance.carryover_policy,
            action_plan=instance.action_plan,
            run_id=run.run_id,
            created_at_ms=run.started_at_ms,
        )
        if configuration_hash(binding) != instance.configuration_hash:
            raise ValueError("strategy instance configuration hash is invalid")
        return binding

    @staticmethod
    def decode(raw: str) -> BrokerBotBinding:
        """Validate v2 bindings and lift the constrained Alpaca v1 shape."""
        payload = json.loads(raw)
        if (
            payload.get("schema_version") == 1
            and payload.get("broker") == "alpaca"
            and isinstance(payload.get("symbol"), str)
            and "action_plan" not in payload
        ):
            payload = {
                **payload,
                "schema_version": 2,
                "action_plan": alpaca_v1_action_plan(payload["symbol"]).model_dump(),
            }
        return BrokerBotBinding.model_validate(payload)
