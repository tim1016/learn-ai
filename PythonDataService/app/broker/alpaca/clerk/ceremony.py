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
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import Any, ClassVar, Protocol

from app.broker.alpaca.clerk.sqlite.operational_files import canonical_json_bytes

DEFAULT_CONFIRMATION_TTL_MS = 120_000
MAX_CONFIRMATION_TTL_MS = 300_000

# Every token this module mints is a sha256 hexdigest, so anything else an
# operator quotes is refused by shape before it is compared -- see the guard in
# ``require_plan_token`` for why the shape check cannot be left to the compare.
_TOKEN = re.compile(r"^[0-9a-f]{64}$")

type Refusal = Callable[[str], Exception]


def require_confirmation_ttl_ms(confirmation_ttl_ms: int, *, refused: Refusal) -> int:
    """Bound the operator-chosen confirmation window, or refuse.

    ``type(...) is not int`` rather than ``isinstance``: ``True`` is an ``int``
    and a boolean TTL is a caller bug, not a one-millisecond window.
    """
    if type(confirmation_ttl_ms) is not int or not 1 <= confirmation_ttl_ms <= MAX_CONFIRMATION_TTL_MS:
        raise refused(f"confirmation TTL must be within 1..{MAX_CONFIRMATION_TTL_MS} ms")
    return confirmation_ttl_ms


class ConfirmablePlan(Protocol):
    """A ceremony's plan: two derived ids over a versioned content shape."""

    __dataclass_fields__: ClassVar[dict[str, Any]]
    schema_version: int
    plan_id: str
    confirmation_token: str


def plan_payload(
    plan: ConfirmablePlan, *, schema_version: int, refused: Refusal, label: str
) -> dict[str, Any]:
    """The plan's content, from which its two ids are derived.

    ``asdict`` recurses into nested evidence dataclasses and maps each tuple
    field to a sequence the canonical encoder writes as the same JSON array.
    The two ids are removed because they *are* the digest of what remains.

    A plan carrying an unknown schema version is refused before it is hashed,
    under the same sentence a failed self-hash gets: the digest of a shape this
    code cannot read says nothing true about it either way.
    """
    if plan.schema_version != schema_version:
        raise refused(f"{label} plan content hash does not verify")
    payload = asdict(plan)
    del payload["plan_id"], payload["confirmation_token"]
    return payload


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

    A token that is not 64 lowercase hex characters is refused by shape before
    the compare, under the same sentence: ``secrets.compare_digest`` raises
    ``TypeError`` on a non-ASCII ``str``, so an operator who typed an accented
    character would otherwise get a traceback out of a CLI whose contract is one
    JSON object per invocation. Refusing by shape leaks nothing the mismatch
    sentence does not already say.
    """
    expected = plan_content_token(payload)
    if plan_id != expected or confirmation_token != expected:
        raise refused(f"{label} plan content hash does not verify")
    if _TOKEN.match(supplied_token) is None or not secrets.compare_digest(supplied_token, expected):
        raise refused(f"{label} confirmation token does not match the plan")


def require_unexpired(*, now_ms: int, expires_at_ms: int, refused: Refusal, label: str) -> None:
    """Refuse a confirmation whose window has closed (inclusive of its last ms)."""
    if now_ms > expires_at_ms:
        raise refused(f"{label} confirmation token has expired")


__all__ = [
    "DEFAULT_CONFIRMATION_TTL_MS",
    "MAX_CONFIRMATION_TTL_MS",
    "ConfirmablePlan",
    "Refusal",
    "plan_content_token",
    "plan_payload",
    "require_confirmation_ttl_ms",
    "require_plan_token",
    "require_unexpired",
]
