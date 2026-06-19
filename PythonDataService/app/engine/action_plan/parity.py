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

Engine ↔ schemas: the input ``ActionPlan`` already crosses this boundary
(schemas → engine), so the output ``ParityWarning`` Pydantic type is
imported from the same place rather than maintained as a parallel
dataclass + translation layer. One source of truth per wire shape.
"""

from __future__ import annotations

from app.schemas.action_plan import ActionPlan, ParityWarning


def parity_diagnostics(plan: ActionPlan) -> list[ParityWarning]:
    """Compute the warning list for a validated ``ActionPlan``.

    Slice 1D ships one warning kind:

    * ``orphan_entry`` — an entry leg whose ``leg_id`` has no matching
      ``close_leg`` on ``on_exit``. The operator is declaring a position
      they have not declared how to close. Warning, not error: calendar /
      roll plans legitimately omit closes.

    Future warning kinds (asymmetric position direction, etc.) extend
    the ``ParityWarning.code`` ``Literal`` union; the HTTP envelope
    shape is stable.
    """

    closed_entry_ids: set[str] = {
        exit_entry.entry_leg_id for exit_entry in plan.on_exit
    }
    return [
        ParityWarning(
            code="orphan_entry",
            message=(
                f"Entry leg {leg.leg_id!r} has no matching close_leg — "
                "operator-declared position will not be closed by this plan."
            ),
            leg_id=leg.leg_id,
        )
        for leg in plan.on_enter
        if leg.leg_id not in closed_entry_ids
    ]
