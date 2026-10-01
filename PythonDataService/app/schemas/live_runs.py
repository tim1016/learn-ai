"""Pydantic v2 schemas surviving the retired live-runs API.

The ``/api/live-runs`` HTTP surface and the host-runner control plane retired
with PR-A/PR-B of #1813; the request/response/daemon-envelope models that
served them were deleted in PR-C. Two top-level models remain:

* ``GateResult`` — the normalized lifecycle gate row (``GateResultStatus`` is
  its intra-file alias)
* ``BotDutyOutcomeView`` — ``app/schemas/broker_bots.py``

All timestamps are int64 milliseconds UTC.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.bot_lifecycle import BotDutyOutcomeKind
from app.utils.session_anchors import MAX_TIMESTAMP_MS

GateResultStatus = Literal[
    "pass",
    "block",
    "poison",
    "freeze",
    "unknown",
    "not_applicable",
]


class GateResult(BaseModel):
    """Canonical lifecycle gate result row.

    A gate result is the enforcement-backed predicate clients can
    render and diagnose. Older readiness rows still expose their
    ``name`` / ``status`` / ``severity`` / ``detail`` fields for
    compatibility; ``GateResult`` is the normalized contract newer
    account-level gates consume.
    """

    model_config = ConfigDict(extra="forbid")

    gate_id: str
    status: GateResultStatus
    source: str
    operator_reason: str
    operator_next_step: str | None = None
    evidence_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)


class BotDutyOutcomeView(BaseModel):
    """Durable terminal duty evidence rendered by the operator surface."""

    model_config = ConfigDict(extra="forbid")

    kind: BotDutyOutcomeKind
    reason_code: str
    recorded_at_ms: int
    run_id: str | None = None
