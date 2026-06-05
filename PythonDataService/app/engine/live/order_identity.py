"""Module A — order identity (deep, pure). ADR-0008 §1, PRD #446.

The single home for "is this order mine?". Mints ``intent_id``s, builds and
parses ``order_ref``s, and classifies ownership by **exact** namespace match
(never a ``startswith`` prefix). No I/O, no broker, no filesystem.

``order_ref = {bot_order_namespace}:{intent_id}`` where
``bot_order_namespace = learn-ai/{strategy_instance_id}/v1``. The ``/v1``
segment versions the wire *encoding* only (ADR-0008 §7); a ``/v2`` bump means
both versions live in the allowed-namespace set during dual-read.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Set as AbstractSet
from enum import StrEnum

# ── Wire-format constants ────────────────────────────────────────────────────
NAMESPACE_ROOT = "learn-ai"
NAMESPACE_VERSION = "v1"
NAMESPACE_SEP = "/"
ORDER_REF_SEP = ":"

# uuid4 -> 16 bytes -> base64url, no padding -> 22 chars.
INTENT_ID_LEN = 22

# Fixed order_ref overhead for the ``learn-ai/{sid}/v1:{intent_id}`` format:
#   "learn-ai/" (9) + "v1" + "/" (3) + ":" (1) + intent_id (22) == 35.
# So len(order_ref) == len(strategy_instance_id) + 35.
ORDER_REF_FIXED_OVERHEAD = (
    len(NAMESPACE_ROOT) + len(NAMESPACE_SEP)
    + len(NAMESPACE_VERSION) + len(NAMESPACE_SEP)
    + len(ORDER_REF_SEP)
    + INTENT_ID_LEN
)

DEFAULT_ORDER_REF_MAX_LENGTH = 60
"""Conservative default cap on ``order_ref`` length.

TODO(#446 Acceptance Gate #1): replace with the value proven by a live paper
order — place one order, read back the stored ``orderRef``, confirm IBKR echoes
the full string untruncated. Truncation is silent and catastrophic, so until
that receipt exists this stays conservative AND activation is refused (see
``broker_ownership_query.require_durable_submit_activation``). A cap of 60
leaves ``len(strategy_instance_id) <= 25``.
"""


class OwnershipRung(StrEnum):
    """Which rung of the ownership ladder matched (ADR-0008 §1). ``order_id``
    is deliberately absent — it never proves ownership."""

    NAMESPACE = "namespace"
    INTENT_ID = "intent_id"
    PERM_ID = "perm_id"
    EXEC_ID = "exec_id"
    NONE = "none"


class OrderRefError(ValueError):
    """Base for ``order_ref`` construction/parsing failures."""


class OrderRefTooLongError(OrderRefError):
    def __init__(self, order_ref: str, max_length: int) -> None:
        super().__init__(
            f"order_ref length {len(order_ref)} exceeds cap {max_length} "
            f"(fail closed; truncation is silent and catastrophic): {order_ref!r}"
        )
        self.order_ref = order_ref
        self.max_length = max_length


class OrderRefParseError(OrderRefError):
    def __init__(self, order_ref: str, detail: str) -> None:
        super().__init__(f"unparseable order_ref ({detail}): {order_ref!r}")
        self.order_ref = order_ref


class InstanceIdTooLongError(ValueError):
    def __init__(
        self, strategy_instance_id: str, max_len: int, order_ref_max_length: int
    ) -> None:
        super().__init__(
            f"strategy_instance_id length {len(strategy_instance_id)} exceeds {max_len} "
            f"(= order_ref cap {order_ref_max_length} - {ORDER_REF_FIXED_OVERHEAD} fixed "
            f"overhead); would overflow the orderRef cap: {strategy_instance_id!r}"
        )
        self.strategy_instance_id = strategy_instance_id
        self.max_len = max_len


def mint_intent_id() -> str:
    """A ``uuid4`` as a 22-char base64url token (no padding).

    The base64url alphabet (``A-Za-z0-9-_``) never contains ``/`` or ``:``, so
    an ``order_ref`` always splits unambiguously on its final ``:``.
    """
    return base64.urlsafe_b64encode(uuid.uuid4().bytes).rstrip(b"=").decode("ascii")


def build_bot_order_namespace(strategy_instance_id: str) -> str:
    """``learn-ai/{strategy_instance_id}/v1`` — the per-instance ownership scope."""
    return (
        f"{NAMESPACE_ROOT}{NAMESPACE_SEP}{strategy_instance_id}"
        f"{NAMESPACE_SEP}{NAMESPACE_VERSION}"
    )


def build_order_ref(
    bot_order_namespace: str,
    intent_id: str,
    *,
    max_length: int = DEFAULT_ORDER_REF_MAX_LENGTH,
) -> str:
    """Compose ``{namespace}:{intent_id}``; **fail closed** over the cap.

    Never truncates — a truncated ``orderRef`` at the broker silently breaks
    ownership (ADR-0008 §1).
    """
    order_ref = f"{bot_order_namespace}{ORDER_REF_SEP}{intent_id}"
    if len(order_ref) > max_length:
        raise OrderRefTooLongError(order_ref, max_length)
    return order_ref


def parse_order_ref(order_ref: str) -> tuple[str, str]:
    """Split on the **final** ``:`` into ``(namespace, intent_id)``.

    base64url intent_ids never contain ``:`` and namespaces never contain
    ``:``, so the final-``:`` split is unambiguous. Empty components are
    rejected (``OrderRefParseError``).
    """
    if ORDER_REF_SEP not in order_ref:
        raise OrderRefParseError(order_ref, "no ':' delimiter")
    namespace, _, intent_id = order_ref.rpartition(ORDER_REF_SEP)
    if not namespace or not intent_id:
        raise OrderRefParseError(order_ref, "empty namespace or intent_id")
    return namespace, intent_id


def order_ref_namespace_matches(
    order_ref: str | None, allowed_namespaces: AbstractSet[str]
) -> bool:
    """True iff ``order_ref`` parses and its namespace is **exactly** in the set.

    Exact equality, never ``startswith``: ``learn-ai/foo/v10:..`` must NOT match
    the namespace ``learn-ai/foo/v1`` (ADR-0008 §1). ``None`` / unparseable →
    not owned.
    """
    if order_ref is None:
        return False
    try:
        namespace, _ = parse_order_ref(order_ref)
    except OrderRefParseError:
        return False
    return namespace in allowed_namespaces


def validate_order_ref_components(
    order_ref: str, bot_order_namespace: str, intent_id: str
) -> bool:
    """For an order **we** placed: validate equality of the stored components.

    No parsing — we hold the components, so we check the invariant
    ``order_ref == f"{namespace}:{intent_id}"`` directly (ADR-0008 §1).
    """
    return order_ref == f"{bot_order_namespace}{ORDER_REF_SEP}{intent_id}"


def classify_ownership(
    *,
    order_ref: str | None,
    perm_id: int | None,
    exec_id: str | None,
    allowed_namespaces: AbstractSet[str],
    known_intent_ids: AbstractSet[str],
    known_perm_ids: AbstractSet[int],
    known_exec_ids: AbstractSet[str],
) -> OwnershipRung:
    """The ownership ladder, in order. ``order_id`` is not a parameter — by
    construction it cannot contribute (ADR-0008 §1):

    1. ``order_ref`` namespace exactly equals an allowed namespace,
    2. known ``intent_id``,
    3. known ``perm_id``,
    4. known ``exec_id``.
    """
    if order_ref is not None:
        try:
            namespace, intent_id = parse_order_ref(order_ref)
        except OrderRefParseError:
            namespace = intent_id = None
        if namespace is not None and namespace in allowed_namespaces:
            return OwnershipRung.NAMESPACE
        if intent_id is not None and intent_id in known_intent_ids:
            return OwnershipRung.INTENT_ID
    if perm_id is not None and perm_id in known_perm_ids:
        return OwnershipRung.PERM_ID
    if exec_id is not None and exec_id in known_exec_ids:
        return OwnershipRung.EXEC_ID
    return OwnershipRung.NONE


def max_strategy_instance_id_len(
    order_ref_max_length: int = DEFAULT_ORDER_REF_MAX_LENGTH,
) -> int:
    """Largest ``strategy_instance_id`` that still fits a full ``order_ref``."""
    return order_ref_max_length - ORDER_REF_FIXED_OVERHEAD


def validate_broker_owned_instance_id(
    strategy_instance_id: str,
    *,
    order_ref_max_length: int = DEFAULT_ORDER_REF_MAX_LENGTH,
) -> str:
    """Reject a ``strategy_instance_id`` too long to fit a full ``order_ref``.

    **Distinct** from ``identity.validate_strategy_instance_id`` (path safety,
    up to 128 chars): this is the tighter broker-ownership constraint
    ``len(sid) <= cap - 35`` (ADR-0008 §1). Returns the value unchanged when it
    fits. Callers should run the path-safety validator too.
    """
    max_len = max_strategy_instance_id_len(order_ref_max_length)
    if len(strategy_instance_id) > max_len:
        raise InstanceIdTooLongError(strategy_instance_id, max_len, order_ref_max_length)
    return strategy_instance_id


__all__ = [
    "DEFAULT_ORDER_REF_MAX_LENGTH",
    "INTENT_ID_LEN",
    "ORDER_REF_FIXED_OVERHEAD",
    "InstanceIdTooLongError",
    "OrderRefError",
    "OrderRefParseError",
    "OrderRefTooLongError",
    "OwnershipRung",
    "build_bot_order_namespace",
    "build_order_ref",
    "classify_ownership",
    "max_strategy_instance_id_len",
    "mint_intent_id",
    "order_ref_namespace_matches",
    "parse_order_ref",
    "validate_broker_owned_instance_id",
    "validate_order_ref_components",
]
