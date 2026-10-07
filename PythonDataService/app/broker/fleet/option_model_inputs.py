"""The coordinator's internal read of the rate and the dividends an option is priced with (#2913).

A Clerk holds no Polygon key and no FRED key (ADR 0076). It asks the
coordinator, which holds both, over the ``/internal/fleet/*`` surface
(``app.routers.internal_fleet``), as it asks for chart history. Like that
read, this is not a ``ProviderOperation``: it belongs to the agent-to-coordinator
family, never to a broker's public catalog.
"""

from __future__ import annotations

INTERNAL_OPTION_MODEL_INPUTS_PATH = "/internal/fleet/option-model-inputs"

#: How long a Clerk waits for the answer. The coordinator asks FRED and Polygon
#: at the same time and waits eight seconds for each
#: (``app.services.option_model_inputs.VENDOR_LOOKUP_BOUND_S``), so its answer
#: is made inside that. An ask that finds its own lookup already running waits
#: to that lookup's bound, and none waits behind a lookup of something else.
#: The rest of the twenty seconds is for a coordinator that is busy.
#: Connecting keeps the fleet default.
OPTION_MODEL_INPUTS_READ_TIMEOUT_S = 20.0

__all__ = ["INTERNAL_OPTION_MODEL_INPUTS_PATH", "OPTION_MODEL_INPUTS_READ_TIMEOUT_S"]
