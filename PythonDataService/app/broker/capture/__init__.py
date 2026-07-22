"""Broker-neutral raw-capture layer (Broker System v2, Layer 2).

Every vendor response — success or error — is journaled verbatim to an
append-only JSONL file before the SDK parses it. The journal is the audit
record and the regeneration source for golden fixtures; no broker-specific
type appears here.
"""

from __future__ import annotations

from app.broker.capture.journal import (
    CaptureEndpoint,
    CaptureJournal,
    get_capture_journal,
    reset_capture_journal_for_testing,
)

__all__ = [
    "CaptureEndpoint",
    "CaptureJournal",
    "get_capture_journal",
    "reset_capture_journal_for_testing",
]
