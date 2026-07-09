"""Durable daily lifecycle state for a strategy instance.

The lifecycle phase is a rebuildable projection, but the operator-owned roster
and explicit retirement marker need a stable home under the existing
``live_state/<strategy_instance_id>/`` directory.
"""

from __future__ import annotations

import contextlib
import os
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.engine.live.identity import validate_strategy_instance_id
from app.engine.live.live_state_sidecar import _file_lock, _fsync_parent_dir


class BotLifecycleStateCorruptError(RuntimeError):
    """Raised when the daily lifecycle state file is unreadable."""

    def __init__(self, path: Path, cause: BaseException) -> None:
        super().__init__(f"daily lifecycle state at {path} is unreadable: {cause}")
        self.path = path
        self.__cause__ = cause


class BotLifecyclePhase(StrEnum):
    OFF_DUTY = "OFF_DUTY"
    ON_DUTY = "ON_DUTY"
    RETIRED = "RETIRED"


class BotLifecycleStateRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    phase: BotLifecyclePhase = BotLifecyclePhase.OFF_DUTY
    on_roster: bool = True
    active_run_id: str | None = None
    last_transition_at_ms: int
    updated_by: str = "system"
    reason: str | None = None
    retired_at_ms: int | None = None
    retired_reason: str | None = None
    replacement_strategy_instance_id: str | None = None
    version: int = 1


class BotRollCallOfferRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    offer_id: str
    strategy_instance_id: str
    run_id: str
    session_date: str
    issued_at_ms: int
    expires_at_ms: int
    status: Literal["active", "consumed"] = "active"
    evidence_snapshot: dict[str, Any] = Field(default_factory=dict)


class BotRollCallOfferLedger(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    offers: list[BotRollCallOfferRecord] = Field(default_factory=list)
    version: int = 1


def stable_bot_lifecycle_state_path(artifacts_root: Path, strategy_instance_id: str) -> Path:
    validate_strategy_instance_id(strategy_instance_id)
    return artifacts_root / "live_state" / strategy_instance_id / "lifecycle_state.json"


def stable_bot_roll_call_offers_path(artifacts_root: Path, strategy_instance_id: str) -> Path:
    validate_strategy_instance_id(strategy_instance_id)
    return artifacts_root / "live_state" / strategy_instance_id / "roll_call_offers.json"


class BotLifecycleStateRepo:
    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> BotLifecycleStateRecord | None:
        if not self._path.exists():
            return None
        try:
            return BotLifecycleStateRecord.model_validate_json(
                self._path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, ValueError) as exc:
            raise BotLifecycleStateCorruptError(self._path, exc) from exc

    def set_roster(
        self,
        on_roster: bool,
        *,
        now_ms: int,
        updated_by: str,
        reason: str | None = None,
    ) -> BotLifecycleStateRecord:
        return self.update(
            now_ms=now_ms,
            updated_by=updated_by,
            phase=None,
            on_roster=on_roster,
            reason=reason,
        )

    def set_phase(
        self,
        phase: BotLifecyclePhase,
        *,
        now_ms: int,
        updated_by: str,
        active_run_id: str | None = None,
        reason: str | None = None,
    ) -> BotLifecycleStateRecord:
        return self.update(
            now_ms=now_ms,
            updated_by=updated_by,
            phase=phase,
            active_run_id=active_run_id,
            reason=reason,
        )

    def retire(
        self,
        *,
        now_ms: int,
        updated_by: str,
        reason: str,
        replacement_strategy_instance_id: str | None = None,
    ) -> BotLifecycleStateRecord:
        return self.update(
            now_ms=now_ms,
            updated_by=updated_by,
            phase=BotLifecyclePhase.RETIRED,
            active_run_id=None,
            on_roster=False,
            reason=reason,
            retired_at_ms=now_ms,
            retired_reason=reason,
            replacement_strategy_instance_id=replacement_strategy_instance_id,
        )

    def update(
        self,
        *,
        now_ms: int,
        updated_by: str,
        phase: BotLifecyclePhase | None = None,
        on_roster: bool | None = None,
        active_run_id: str | None = None,
        reason: str | None = None,
        retired_at_ms: int | None = None,
        retired_reason: str | None = None,
        replacement_strategy_instance_id: str | None = None,
    ) -> BotLifecycleStateRecord:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(self._path):
            existing = self.read()
            next_phase = (
                phase
                if phase is not None
                else (existing.phase if existing is not None else BotLifecyclePhase.OFF_DUTY)
            )
            next_active_run_id = _next_active_run_id(
                phase=phase,
                explicit_active_run_id=active_run_id,
                existing=existing,
            )
            record = BotLifecycleStateRecord(
                phase=next_phase,
                on_roster=(
                    on_roster
                    if on_roster is not None
                    else (existing.on_roster if existing is not None else True)
                ),
                active_run_id=next_active_run_id,
                last_transition_at_ms=now_ms,
                updated_by=updated_by,
                reason=reason,
                retired_at_ms=(
                    retired_at_ms
                    if retired_at_ms is not None
                    else (existing.retired_at_ms if existing is not None else None)
                ),
                retired_reason=(
                    retired_reason
                    if retired_reason is not None
                    else (existing.retired_reason if existing is not None else None)
                ),
                replacement_strategy_instance_id=(
                    replacement_strategy_instance_id
                    if replacement_strategy_instance_id is not None
                    else (existing.replacement_strategy_instance_id if existing is not None else None)
                ),
                version=(existing.version + 1) if existing is not None else 1,
            )
            self._write_locked(record)
            return record

    def _write_locked(self, record: BotLifecycleStateRecord) -> None:
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = record.model_dump_json().encode("utf-8")
        with open(tmp_path, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.replace(tmp_path, self._path)
        except Exception:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise
        _fsync_parent_dir(self._path)


class BotRollCallOfferRepo:
    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> BotRollCallOfferLedger:
        if not self._path.exists():
            return BotRollCallOfferLedger()
        try:
            return BotRollCallOfferLedger.model_validate_json(
                self._path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, ValueError) as exc:
            raise BotLifecycleStateCorruptError(self._path, exc) from exc

    def active_offer(self, *, now_ms: int, session_date: str | None = None) -> BotRollCallOfferRecord | None:
        ledger = self.read()
        for offer in reversed(ledger.offers):
            if session_date is not None and offer.session_date != session_date:
                continue
            if offer.status == "active" and offer.issued_at_ms <= now_ms < offer.expires_at_ms:
                return offer
        return None

    def append(self, offer: BotRollCallOfferRecord) -> BotRollCallOfferRecord:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(self._path):
            ledger = self.read()
            next_ledger = BotRollCallOfferLedger(
                offers=[*ledger.offers, offer],
                version=ledger.version + 1,
            )
            self._write_locked(next_ledger)
        return offer

    def consume(self, offer_id: str) -> BotRollCallOfferRecord | None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(self._path):
            ledger = self.read()
            matched: BotRollCallOfferRecord | None = None
            offers: list[BotRollCallOfferRecord] = []
            for offer in ledger.offers:
                if offer.offer_id == offer_id and offer.status == "active":
                    matched = offer.model_copy(update={"status": "consumed"})
                    offers.append(matched)
                else:
                    offers.append(offer)
            if matched is None:
                return None
            self._write_locked(
                BotRollCallOfferLedger(
                    offers=offers,
                    version=ledger.version + 1,
                )
            )
            return matched

    def _write_locked(self, ledger: BotRollCallOfferLedger) -> None:
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = ledger.model_dump_json().encode("utf-8")
        with open(tmp_path, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.replace(tmp_path, self._path)
        except Exception:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise
        _fsync_parent_dir(self._path)


def _next_active_run_id(
    *,
    phase: BotLifecyclePhase | None,
    explicit_active_run_id: str | None,
    existing: BotLifecycleStateRecord | None,
) -> str | None:
    if explicit_active_run_id is not None:
        return explicit_active_run_id
    if phase in {BotLifecyclePhase.OFF_DUTY, BotLifecyclePhase.RETIRED}:
        return None
    return existing.active_run_id if existing is not None else None


__all__ = [
    "BotLifecyclePhase",
    "BotLifecycleStateCorruptError",
    "BotLifecycleStateRecord",
    "BotLifecycleStateRepo",
    "BotRollCallOfferRecord",
    "BotRollCallOfferRepo",
    "stable_bot_lifecycle_state_path",
    "stable_bot_roll_call_offers_path",
]
