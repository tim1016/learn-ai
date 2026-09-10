"""User-owned broker configuration profiles (ADR 0060, plan package B).

The persistence layer and API surface behind
``/api/brokers/alpaca/configuration``: a dedicated versioned SQLite database on
the Clerk volume holding the local owner, named profiles, their immutable
revisions, account nicknames, the installation selection with its one-shot
Apply request, and an append-only configuration event log.

Deliberately *not* here: credential resolution and broker account verification
(package C — see ``seams.py``), and startup resolution of the effective
selection into a running worker (package D). Nothing in this package changes
broker or worker startup behaviour.
"""

from __future__ import annotations

from app.broker_configuration.envelope import ValidatedLiveEnvelope
from app.broker_configuration.errors import BrokerConfigurationError, ProfilesDatabaseUnavailable
from app.broker_configuration.runtime import (
    build_service,
    get_broker_configuration_service,
    reset_broker_configuration_service_for_testing,
)
from app.broker_configuration.service import BrokerConfigurationService

__all__ = [
    "BrokerConfigurationError",
    "BrokerConfigurationService",
    "ProfilesDatabaseUnavailable",
    "ValidatedLiveEnvelope",
    "build_service",
    "get_broker_configuration_service",
    "reset_broker_configuration_service_for_testing",
]
