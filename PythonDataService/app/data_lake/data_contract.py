"""Deterministic data-contract fingerprint.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 3.1
("data_contract_hash" — proves same-contract identity at the catalog level).

`data_contract_hash` is sha256 over canonical JSON of:
  {provider, provider_params, price_adjustment_mode, session_policy,
   lean_format_version}

The hash is stable across nested key ordering thanks to `sort_keys=True`.
Two artifacts with the same hash are interchangeable consumers of the same
contract; the unique constraint enforces (market, symbol, ...) uniqueness on
top of that.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def data_contract_hash(
    provider: str,
    provider_params: dict[str, Any],
    price_adjustment_mode: str | None,
    session_policy: str,
    lean_format_version: int,
) -> str:
    """Compute the 64-char hex sha256 of the canonical-JSON fingerprint."""
    payload = {
        "provider": provider,
        "provider_params": provider_params,
        "price_adjustment_mode": price_adjustment_mode,
        "session_policy": session_policy,
        "lean_format_version": lean_format_version,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
