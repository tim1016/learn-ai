"""Locating the profiles database, and the one service the process shares.

The Clerk directory is *deployment bootstrap*: it must exist before a profile
can be loaded, and it is the thing that locates the profiles database (ADR 0060
Decision 1). ``AlpacaSettings.clerk_dir`` remains its canonical declaration —
this module reads the same variable with the same default, deliberately
duplicating it for one reason:

    ``AlpacaSettings`` requires ``ALPACA_API_KEY_ID`` and
    ``ALPACA_API_SECRET_KEY``, so instantiating it to learn a *directory* makes
    reading a profile depend on credentials being present. ADR 0060 Decision 7
    says the opposite: missing configuration closes the gate and surfaces a
    reason, it never crash-loops and it never couples an unrelated read to a
    credential.

Per CLAUDE.md guiding philosophy #5 the duplicate carries a parity test naming
the canonical file: ``tests/broker_configuration/test_clerk_dir_parity.py``
pins this resolver against ``AlpacaSettings.clerk_dir`` for both the default
and an overridden ``ALPACA_CLERK_DIR``.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.broker_configuration.service import BrokerConfigurationService
from app.broker_configuration.store import ProfilesStore
from app.config import settings

CLERK_DIR_ENV_VAR = "ALPACA_CLERK_DIR"
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CLERK_DIR = _SERVICE_ROOT / "artifacts" / "alpaca_clerk"

_service: BrokerConfigurationService | None = None


def resolve_clerk_dir() -> Path:
    """The Clerk directory, without requiring Alpaca credentials to exist."""
    configured = os.environ.get(CLERK_DIR_ENV_VAR)
    if configured is None or not configured.strip():
        return DEFAULT_CLERK_DIR
    return Path(configured)


def build_service(*, clerk_dir: Path | None = None) -> BrokerConfigurationService:
    """Open the profiles database and wrap it in the configuration service."""
    root = resolve_clerk_dir() if clerk_dir is None else clerk_dir
    return BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=root),
        operator_identity=settings.PANEL_OPERATOR_IDENTITY,
    )


def get_broker_configuration_service() -> BrokerConfigurationService:
    """The process-wide service, opened on first use.

    Lazy like ``get_alpaca_settings``: an unreadable database must refuse the
    request that touched it, not prevent the service from booting.
    """
    global _service
    if _service is None:
        _service = build_service()
    return _service


def reset_broker_configuration_service_for_testing() -> None:
    """Drop the cached service so a test can rebind the Clerk directory."""
    global _service
    if _service is not None:
        _service.close()
    _service = None


__all__ = [
    "CLERK_DIR_ENV_VAR",
    "DEFAULT_CLERK_DIR",
    "build_service",
    "get_broker_configuration_service",
    "reset_broker_configuration_service_for_testing",
    "resolve_clerk_dir",
]
