"""Pure parity diagnostics — PRD #593 Slice 1D (issue #597).

Computes non-blocking warnings on a validated ``ActionPlan``. Hard schema
rejections (orphan ``close_leg``, duplicate ``leg_id``, etc.) live in
Pydantic; parity *warnings* (asymmetric structures, orphan entries) live
here. Backed by the same function the HTTP preview endpoint exposes —
operators can submit a plan with warnings (override path); only schema
errors block.

ADR 0012 §"Architectural decisions": parity is a preview-endpoint
concern, NOT a schema concern. This function does NOT consult
``live_config.symbol``, the instance roster, or any other session state.
The plan supplies all context via explicit ``instrument.underlying`` (ADR
0012 §5).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.schemas.action_plan import ActionPlan


class ParityWarningCode(StrEnum):
    """Single source of truth for the warning-code enum. The HTTP layer
    serializes via the string value; the frontend's discriminated union
    of warning shapes keys off the same code."""

    orphan_entry = "orphan_entry"


@dataclass(frozen=True, slots=True)
class ParityWarning:
    code: ParityWarningCode
    message: str
    # The originating ``leg_id`` when locatable; ``None`` for plan-level
    # warnings without a single anchor leg.
    leg_id: str | None


def parity_diagnostics(plan: ActionPlan) -> list[ParityWarning]:
    """Compute the warning list for a validated ``ActionPlan``.

    Slice 1D ships one warning kind:

    * ``orphan_entry`` — an entry leg whose ``leg_id`` has no matching
      ``close_leg`` on ``on_exit``. The operator is declaring a position
      they have not declared how to close. Warning, not error: calendar /
      roll plans legitimately omit closes.

    Future warning kinds (asymmetric position direction, etc.) extend the
    enum; the HTTP envelope shape is stable.
    """

    closed_entry_ids: set[str] = {
        exit_entry.entry_leg_id for exit_entry in plan.on_exit
    }
    warnings: list[ParityWarning] = []
    for leg in plan.on_enter:
        if leg.leg_id not in closed_entry_ids:
            warnings.append(
                ParityWarning(
                    code=ParityWarningCode.orphan_entry,
                    message=(
                        f"Entry leg {leg.leg_id!r} has no matching close_leg — "
                        "operator-declared position will not be closed by this plan."
                    ),
                    leg_id=leg.leg_id,
                )
            )
    return warnings
