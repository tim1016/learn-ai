"""Shared qualification hashing and timestamp bounds used by Alpaca SQLite."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Literal

PaperQualificationStatus = Literal["NOT_RUN"]


def account_custody_qualification_payload_sha256(payload: Mapping[str, object]) -> str:
    """Hash a qualification payload with deterministic JSON bytes."""

    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "PaperQualificationStatus",
    "account_custody_qualification_payload_sha256",
]
