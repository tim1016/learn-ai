"""The two-step confirmation ceremony every supervised operator action shares.

``cutover.py`` invented the shape: a read-only ``plan`` whose content hash *is*
its confirmation token, a bounded confirmation window, and an ``apply`` that
re-checks both before it re-observes anything. Slice 6's arming ceremony (ADR
0059 D3) is the second one, so the bounds and the three checks live here once
instead of being copied -- a correction to any of them then lands in one place
rather than protecting only one ceremony.

Each caller keeps what is genuinely its own: its plan dataclass, the payload it
hashes, and the error it refuses with, passed in as ``refused`` and ``label``.
``refused`` is a callable rather than an exception class because the arming
ceremony's refusal carries a reason code as well as a message.

The digest is ``operational_files.canonical_json_bytes`` -- canonical JSON with
a trailing newline -- because that is the exact byte sequence ``cutover.py``
has always hashed. It is deliberately *not* ``sealed_ledger.canonical_sha256``,
which omits the newline: changing it would invalidate every plan token an
operator is holding.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Mapping
from typing import Any

from app.broker.alpaca.clerk.sqlite.operational_files import canonical_json_bytes

DEFAULT_CONFIRMATION_TTL_MS = 120_000
MAX_CONFIRMATION_TTL_MS = 300_000

type Refusal = Callable[[str], Exception]


def require_confirmation_ttl_ms(confirmation_ttl_ms: int, *, refused: Refusal) -> int:
    """Bound the operator-chosen confirmation window, or refuse.

    ``type(...) is not int`` rather than ``isinstance``: ``True`` is an ``int``
    and a boolean TTL is a caller bug, not a one-millisecond window.
    """
    if type(confirmation_ttl_ms) is not int or not 1 <= confirmation_ttl_ms <= MAX_CONFIRMATION_TTL_MS:
        raise refused(f"confirmation TTL must be within 1..{MAX_CONFIRMATION_TTL_MS} ms")
    return confirmation_ttl_ms


def plan_content_token(payload: Mapping[str, Any]) -> str:
    """The plan's own content hash, which is also its confirmation token."""
    return hashlib.sha256(canonical_json_bytes(dict(payload))).hexdigest()


def require_plan_token(
    payload: Mapping[str, Any],
    *,
    plan_id: str,
    confirmation_token: str,
    supplied_token: str,
    refused: Refusal,
    label: str,
) -> None:
    """Verify the plan hashes to its own two ids, and that the operator quoted them.

    The self-hash check comes first: a plan whose content no longer matches its
    ids is a forged or mutated plan, and comparing a token against it would
    answer a question about the wrong document.
    """
    expected = plan_content_token(payload)
    if plan_id != expected or confirmation_token != expected:
        raise refused(f"{label} plan content hash does not verify")
    if not secrets.compare_digest(supplied_token, expected):
        raise refused(f"{label} confirmation token does not match the plan")


def require_unexpired(*, now_ms: int, expires_at_ms: int, refused: Refusal, label: str) -> None:
    """Refuse a confirmation whose window has closed (inclusive of its last ms)."""
    if now_ms > expires_at_ms:
        raise refused(f"{label} confirmation token has expired")


__all__ = [
    "DEFAULT_CONFIRMATION_TTL_MS",
    "MAX_CONFIRMATION_TTL_MS",
    "Refusal",
    "plan_content_token",
    "require_confirmation_ttl_ms",
    "require_plan_token",
    "require_unexpired",
]
