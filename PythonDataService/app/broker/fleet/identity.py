"""Opaque, backend-issued identities for the broker clerk fleet.

PRD §6 and ADR 0062 Decision 3: a ``clerk_id`` is stable, opaque and
non-semantic; callers never mint or parse it, and retirement is terminal.
"Opaque" is a contract about *meaning*, not about syntax: every identifier is
``<prefix>_<hex>`` so a boundary can reject a forged, truncated or
wrong-family value without learning anything about the clerk it names. The
format check is the only parsing anyone — including this package — performs.
"""

from __future__ import annotations

import re
import secrets

_CLERK_ID = re.compile(r"^clrk_[0-9a-f]{24}$")
_VOLUME_ID = re.compile(r"^vol_[0-9a-f]{24}$")
_WORKER_KEY = re.compile(r"^wkrk_[0-9a-f]{32}$")
_AGENT_INSTANCE_ID = re.compile(r"^agnt_[0-9a-f]{24}$")
_CORRELATION_ID = re.compile(r"^corr_[0-9a-f]{24}$")
_SERVICE_TOKEN = re.compile(r"^svct_[0-9a-f]{32}$")


def new_clerk_id() -> str:
    """A fresh opaque clerk identity; never derived from an account or label."""
    return f"clrk_{secrets.token_hex(12)}"


def new_volume_id() -> str:
    """A fresh opaque volume identity bound to one physical mounted root."""
    return f"vol_{secrets.token_hex(12)}"


def new_worker_key() -> str:
    """A fresh durable worker identity (ADR 0060's deferred mechanism, designed).

    Longer than the public identifiers because it never crosses the public
    API (PRD FR-012) and is compared with ``hmac.compare_digest`` where an
    agent presents it.
    """
    return f"wkrk_{secrets.token_hex(16)}"


def new_agent_instance_id() -> str:
    """A fresh ephemeral identity for one agent process registration."""
    return f"agnt_{secrets.token_hex(12)}"


def new_correlation_id() -> str:
    """A fresh correlation identity for one routing attempt."""
    return f"corr_{secrets.token_hex(12)}"


def new_service_token() -> str:
    """A fresh environment-only internal service token (audit 2026-09-13, finding 3).

    Transport credential, never durable identity: the value is minted by a
    host ceremony, persisted only in the operator's uncommitted environment
    files on both ends, and never stored in a registry, receipt or log. The
    ``worker_key`` stays the stored durable identity and is never used to
    authenticate a transport.
    """
    return f"svct_{secrets.token_hex(16)}"


def is_clerk_id(value: object) -> bool:
    """Whether the value is a well-formed opaque clerk identity."""
    return isinstance(value, str) and _CLERK_ID.fullmatch(value) is not None


def is_volume_id(value: object) -> bool:
    """Whether the value is a well-formed opaque volume identity."""
    return isinstance(value, str) and _VOLUME_ID.fullmatch(value) is not None


def is_worker_key(value: object) -> bool:
    """Whether the value is a well-formed opaque worker key."""
    return isinstance(value, str) and _WORKER_KEY.fullmatch(value) is not None


def is_agent_instance_id(value: object) -> bool:
    """Whether the value is a well-formed agent instance identity."""
    return isinstance(value, str) and _AGENT_INSTANCE_ID.fullmatch(value) is not None


def is_correlation_id(value: object) -> bool:
    """Whether the value is a well-formed correlation identity."""
    return isinstance(value, str) and _CORRELATION_ID.fullmatch(value) is not None


def is_service_token(value: object) -> bool:
    """Whether the value is a well-formed internal service token."""
    return isinstance(value, str) and _SERVICE_TOKEN.fullmatch(value) is not None


__all__ = [
    "is_agent_instance_id",
    "is_clerk_id",
    "is_correlation_id",
    "is_service_token",
    "is_volume_id",
    "is_worker_key",
    "new_agent_instance_id",
    "new_clerk_id",
    "new_correlation_id",
    "new_service_token",
    "new_volume_id",
    "new_worker_key",
]
