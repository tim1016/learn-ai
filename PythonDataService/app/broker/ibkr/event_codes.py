"""Shared IBKR event-code vocabulary.

The live client, recovery logic, and broker session mirror all import these
sets so code meanings do not drift between safety behavior and observability.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

BrokerSessionEventCategory = Literal[
    "client_lifecycle",
    "link_connectivity",
    "recovery_reconnect",
    "data_farm",
    "auth_session",
    "order_execution",
    "pacing_throttling",
    "fault_client_error",
    "unclassified",
]
BrokerSessionEventSeverity = Literal["info", "warning", "critical"]


@dataclass(frozen=True)
class IbkrCodeMeaning:
    category: BrokerSessionEventCategory
    severity: BrokerSessionEventSeverity
    label: str


# TWS/IB connectivity error codes that the ``errorEvent`` handler reacts to.
# 1100 = "Connectivity between IB and TWS has been lost"; 504 = "Not
# connected". Both mean the data feed is dead even though the API socket to TWS
# may still report ``isConnected() == True``. 1101/1102 are restored signals.
CONNECTIVITY_LOST_CODES = frozenset({1100, 1300, 2110, 504})
CONNECTIVITY_RESTORED_CODES = frozenset({1101, 1102})
SUBSCRIPTIONS_STALE_CODES = frozenset({1101})
DATA_FARM_DEGRADED_CODES = frozenset({2103, 2105})
DATA_FARM_OK_CODES = frozenset({2104, 2106})

IBKR_CODE_MEANINGS: dict[int, IbkrCodeMeaning] = {
    1100: IbkrCodeMeaning(
        category="link_connectivity",
        severity="warning",
        label="IBKR link interrupted",
    ),
    1101: IbkrCodeMeaning(
        category="link_connectivity",
        severity="info",
        label="IBKR link restored; subscriptions stale",
    ),
    1102: IbkrCodeMeaning(
        category="link_connectivity",
        severity="info",
        label="IBKR link restored",
    ),
    1300: IbkrCodeMeaning(
        category="link_connectivity",
        severity="warning",
        label="IBKR socket port reset",
    ),
    2110: IbkrCodeMeaning(
        category="link_connectivity",
        severity="warning",
        label="IBKR server link interrupted",
    ),
    504: IbkrCodeMeaning(
        category="link_connectivity",
        severity="warning",
        label="IBKR API client not connected",
    ),
    2103: IbkrCodeMeaning(
        category="data_farm",
        severity="warning",
        label="Market data farm degraded",
    ),
    2104: IbkrCodeMeaning(
        category="data_farm",
        severity="info",
        label="Market data farm restored",
    ),
    2105: IbkrCodeMeaning(
        category="data_farm",
        severity="warning",
        label="Historical data farm degraded",
    ),
    2106: IbkrCodeMeaning(
        category="data_farm",
        severity="info",
        label="Historical data farm restored",
    ),
}
