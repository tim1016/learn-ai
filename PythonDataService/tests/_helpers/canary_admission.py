"""Test-only admission of one canary (program_key, account_id) pairing.

See ``app.services.canary_admission``'s module docstring for the full
picture: ``CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS`` gates every real Alpaca
Paper (``mode="trade"``) admission of a Signal-Program-backed strategy to an
exact ``(program_key, account_id)`` pairing, and it ships as an empty
``frozenset()`` on purpose. Populating it is a deliberate human decision made
after reviewing the full composed proof for one specific program bound to
one specific account -- never something code does on its own, and never a
default. ``pytest.MonkeyPatch`` is the only acceptable way to admit a
pairing outside that decision, and only for the lifetime of one test.

Most callers reach for this only to get *past* the gate so the rest of the
test can exercise something unrelated (registry/Clerk ordering, decision
receipts, warmup backfill, catalog demotion, ...); the allowlist mechanism
itself is covered directly by ``tests/services/test_canary_admission.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


def admit_canary_pairing(monkeypatch: pytest.MonkeyPatch, program_key: str, account_id: str) -> None:
    """Monkeypatch the allowlist open for exactly one ``(program_key, account_id)`` pairing.

    Both arguments are required with no default -- a caller must name the
    one pairing its test actually needs. There is no "admit everything"
    convenience mode: that would defeat the point of an allowlist that ships
    empty by design.
    """
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({(program_key, account_id)}),
    )
