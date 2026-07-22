"""Durable evidence for a Clerk-owned end-of-day clock-out."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.engine.live.command_channel import AckedCommand, CommandVerb
from app.engine.live.live_state_sidecar import _fsync_parent_dir

CLOCK_OUT_RECEIPT_FILENAME = "clock_out_receipt.json"


class ClockOutReceiptCorruptError(RuntimeError):
    """Raised when a clock-out receipt cannot be trusted."""

    def __init__(self, path: Path, cause: BaseException) -> None:
        super().__init__(f"clock-out receipt at {path} is unreadable: {cause}")
        self.path = path
        self.__cause__ = cause


class ClockOutReceipt(BaseModel):
    """Broker-primary evidence for the validation strategy's daily exit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1, max_length=128)
    strategy_instance_id: str = Field(min_length=1, max_length=128)
    command_seq: int = Field(ge=1)
    status: Literal["flat", "not_flat", "failed"]
    requested_at_ms: int = Field(ge=0)
    completed_at_ms: int = Field(ge=0)
    broker_evidence_at_ms: int | None = Field(default=None, ge=0)
    stop_persisted_at_ms: int | None = Field(default=None, ge=0)
    broker_positions: dict[str, float] = Field(default_factory=dict)
    clerk_order_count: int = Field(default=0, ge=0)
    reason_code: str = Field(min_length=1, max_length=128)


def clock_out_receipt_path(run_dir: Path) -> Path:
    return run_dir / CLOCK_OUT_RECEIPT_FILENAME


def read_clock_out_receipt(run_dir: Path) -> ClockOutReceipt | None:
    path = clock_out_receipt_path(run_dir)
    if not path.exists():
        return None
    try:
        return ClockOutReceipt.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise ClockOutReceiptCorruptError(path, exc) from exc


def write_clock_out_receipt(run_dir: Path, receipt: ClockOutReceipt) -> Path:
    """Atomically persist terminal end-day evidence before process exit."""

    path = clock_out_receipt_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(receipt.model_dump_json().encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
    _fsync_parent_dir(path)
    return path


def clock_out_completion_is_durable(run_dir: Path, receipt: ClockOutReceipt) -> bool:
    """Return whether the completed command ack proves this receipt can end duty."""

    if receipt.status != "flat" or receipt.stop_persisted_at_ms is None:
        return False
    ack_path = run_dir / "commands" / f"command.{receipt.command_seq}.{CommandVerb.CLOCK_OUT.value}.ack.json"
    try:
        ack = AckedCommand.model_validate_json(ack_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError):
        return False
    return (
        ack.seq == receipt.command_seq
        and ack.verb is CommandVerb.CLOCK_OUT
        and ack.outcome.get("status") == "completed"
        and ack.outcome.get("effect") == "clocked_out_flat"
    )


def clock_out_is_in_progress(run_dir: Path) -> bool:
    """Project a queued or accepted clock-out until its command reaches a final ack."""

    commands_dir = run_dir / "commands"
    if any(commands_dir.glob(f"command.*.{CommandVerb.CLOCK_OUT.value}.pending.json")):
        return True
    saw_accepted = False
    saw_already_running = False
    saw_terminal = False
    for ack_path in commands_dir.glob(f"command.*.{CommandVerb.CLOCK_OUT.value}.ack.json"):
        try:
            ack = AckedCommand.model_validate_json(ack_path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError):
            continue
        if ack.outcome.get("status") in {"completed", "failed", "error"}:
            saw_terminal = True
        elif ack.outcome.get("status") == "accepted":
            saw_accepted = True
        elif ack.outcome.get("status") == "already_running":
            saw_already_running = True
    # A later retry has its own accepted ack and remains in progress. An
    # ``already_running`` follower, however, belongs to an earlier leader and
    # cannot outlive that leader's terminal evidence.
    return saw_accepted or (saw_already_running and not saw_terminal)
