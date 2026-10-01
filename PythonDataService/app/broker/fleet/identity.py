"""Opaque, backend-issued identities for the broker clerk fleet.

ADR 0062 Decision 3: a ``clerk_id`` is stable, opaque and
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


def new_clerk_id() -> str:
    """A fresh opaque clerk identity; never derived from an account or label."""
    return f"clrk_{secrets.token_hex(12)}"


def new_volume_id() -> str:
    """A fresh opaque volume identity bound to one physical mounted root."""
    return f"vol_{secrets.token_hex(12)}"


def new_worker_key() -> str:
    """A fresh durable worker identity (ADR 0060's deferred mechanism, designed).

    Longer than the public identifiers because it never crosses the public
    API (ADR 0062 Decision 3) and is compared with ``hmac.compare_digest`` where an
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
    """A fresh environment-only internal service token (ADR 0062 addendum, item 5).

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


__all__ = [
    "is_clerk_id",
    "new_agent_instance_id",
    "new_clerk_id",
    "new_correlation_id",
    "new_service_token",
    "new_volume_id",
    "new_worker_key",
]
