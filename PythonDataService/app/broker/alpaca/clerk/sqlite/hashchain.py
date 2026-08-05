"""Hash-chain row format — pinned byte-for-byte in the contracts doc §7.

``row_hash = H(prev_hash || canonical(payload))``, where ``||`` is UTF-8
string concatenation (not raw hash-byte concatenation) and ``canonical``
is a fixed-key-order, no-whitespace JSON serialization. See
``docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md`` §7 for the full
rationale; this module is the canonical implementation of that pin.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

GENESIS = "GENESIS"

# The custody_transitions columns that participate in the hash, in the order
# listed in the pinned contracts doc §11 / schema §3 — everything except
# sequence, prev_hash, row_hash themselves.
PAYLOAD_COLUMNS: tuple[str, ...] = (
    "authority_generation",
    "strategy_instance_id",
    "run_id",
    "command_id",
    "effect_operation_id",
    "order_ref",
    "broker_order_id",
    "transition_kind",
    "custody_owner",
    "execution_authority",
    "operation_state",
    "broker_state",
    "proof_reference",
    "source_event_at_ms",
    "clerk_observed_at_ms",
    "recorded_at_ms",
    "summary_code",
    "facts_schema_version",
    "facts_json",
)


def canonicalize(obj: Any) -> str:
    """The one canonicalization routine every hashed payload goes through.

    Sorted keys, no whitespace, ASCII-escaped — used both for
    ``custody_transitions`` payloads and, per §7's ``facts_json`` rule, for
    ``facts_json`` itself before it is embedded as a string value.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_payload(row: Mapping[str, Any]) -> str:
    """Build the canonical JSON string for a ``custody_transitions`` row.

    Every column in ``PAYLOAD_COLUMNS`` is present as a key, missing/``None``
    values serialize as JSON ``null`` — never omitted (§7).
    """
    missing = [c for c in PAYLOAD_COLUMNS if c not in row]
    if missing:
        raise ValueError(f"payload missing required columns: {missing}")
    payload = {column: row[column] for column in PAYLOAD_COLUMNS}
    return canonicalize(payload)


def compute_row_hash(prev_hash: str, payload_canonical: str) -> str:
    """``sha256(prev_hash + payload_canonical)`` as a lowercase hex digest."""
    return hashlib.sha256((prev_hash + payload_canonical).encode("utf-8")).hexdigest()


def verify_chain(rows: list[Mapping[str, Any]]) -> int | None:
    """Recompute each row's hash in sequence order; return the first bad sequence.

    ``rows`` must be ordered by ``sequence`` ascending and each row must
    contain ``sequence``, ``prev_hash``, ``row_hash`` plus every
    ``PAYLOAD_COLUMNS`` entry. Returns ``None`` if the whole chain verifies,
    otherwise the ``sequence`` of the first row whose stored ``row_hash``
    disagrees with the recomputed one.
    """
    expected_prev = GENESIS
    for row in rows:
        payload = canonical_payload(row)
        expected_hash = compute_row_hash(expected_prev, payload)
        if row["prev_hash"] != expected_prev or row["row_hash"] != expected_hash:
            return int(row["sequence"])
        expected_prev = row["row_hash"]
    return None
