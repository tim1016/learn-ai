"""The coordinator's internal read of the rate and the dividends an option is priced with (#2913).

A Clerk holds no Polygon key and no FRED key (ADR 0062). It asks the
coordinator, which holds both, over the ``/internal/fleet/*`` surface
(``app.routers.internal_fleet``), as it asks for chart history. Like that
read, this is not a ``ProviderOperation``: it belongs to the agent-to-coordinator
family, never to a broker's public catalog.
"""

from __future__ import annotations

INTERNAL_OPTION_MODEL_INPUTS_PATH = "/internal/fleet/option-model-inputs"

#: How long a Clerk waits for the answer. The coordinator may ask FRED for four
#: series and Polygon for one list before it has one, and an ask that waits may
#: stand behind another Clerk's lookup first. Connecting keeps the fleet default.
OPTION_MODEL_INPUTS_READ_TIMEOUT_S = 20.0

__all__ = ["INTERNAL_OPTION_MODEL_INPUTS_PATH", "OPTION_MODEL_INPUTS_READ_TIMEOUT_S"]
