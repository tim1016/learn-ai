"""Shared IBKR event-code vocabulary.

``app/broker/ibkr/client.py`` imports these sets so its connectivity and
data-farm handling reacts to a single shared definition of each code.
"""

from __future__ import annotations

# TWS/IB connectivity error codes that the ``errorEvent`` handler reacts to.
# 1100 = "Connectivity between IB and TWS has been lost"; 504 = "Not
# connected". Both mean the data feed is dead even though the API socket to TWS
# may still report ``isConnected() == True``. 507 is a socket framing failure
# called out by ADR 0018's SOCKET_DOWN transition. 1101/1102 are restored
# signals.
CONNECTIVITY_LOST_CODES = frozenset({1100, 1300, 2110, 504, 507})
CONNECTIVITY_RESTORED_CODES = frozenset({1101, 1102})
SUBSCRIPTIONS_STALE_CODES = frozenset({1101})
DATA_FARM_DEGRADED_CODES = frozenset({2103, 2105})
DATA_FARM_OK_CODES = frozenset({2104, 2106})
