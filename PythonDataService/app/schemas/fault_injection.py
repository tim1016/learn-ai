"""Request/response schemas for the dev-only fault-injection arm endpoint (PRD #1354).

Transport DTOs only; the behavior lives in
``app.broker.alpaca.fault_injection``. These are exposed exclusively when the
seam is enabled (``ALPACA_FAULT_INJECTION_ENABLED``), so they never reach a
normal deployment's schema.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.fault_injection import FrameFaultKind, WriteFaultKind


class ArmFaultRequest(BaseModel):
    """Arm one fault against the running data plane.

    ``kind`` must be a known write or frame fault. ``target`` scopes it — for a
    write fault it is matched (substring) against the submit ``client_order_id``
    or the cancel ``order_id``; for a frame fault it is the ``client_order_id``
    the crafted frame is attributed to. ``count`` bounds how many times the
    fault fires. ``params`` carries frame-shaping overrides (``symbol`` / ``qty``
    / ``price`` / ``execution_id``) and is ignored for write faults.
    """

    model_config = ConfigDict(extra="forbid")

    kind: WriteFaultKind | FrameFaultKind
    target: str | None = None
    count: int = Field(default=1, ge=1, le=100)
    params: dict[str, str] = Field(default_factory=dict)


class ArmedFaultView(BaseModel):
    """One currently-armed fault."""

    kind: str
    target: str | None
    remaining: int
    params: dict[str, str]


class FaultInjectionStatus(BaseModel):
    """Whether the seam is permitted plus every currently-armed fault."""

    permitted: bool
    write: list[ArmedFaultView]
    frame: list[ArmedFaultView]


class FaultInjectionDisarmResult(BaseModel):
    """How many armed faults were cleared."""

    cleared: int
