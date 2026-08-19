"""Phase 3 / VCR-0006 — retained ledger↔broker identity helpers.

The pure normalization and comparison rules remain useful for read evidence;
their former LiveEngine consumer retired with #1583.
"""

from __future__ import annotations

import pytest


def test_normalize_uppercases_and_strips() -> None:
    """``raw.strip().upper()`` — case-insensitive, whitespace-tolerant."""
    from app.engine.live.account_identity import normalize_account_id

    assert normalize_account_id("  du1234567  ") == "DU1234567"
    assert normalize_account_id("DU1234567") == "DU1234567"
    assert normalize_account_id("du1234567") == "DU1234567"


def test_normalize_rejects_internal_whitespace() -> None:
    from app.engine.live.account_identity import (
        InvalidAccountIdError,
        normalize_account_id,
    )

    with pytest.raises(InvalidAccountIdError, match="whitespace"):
        normalize_account_id("DU 1234567")


def test_normalize_rejects_non_alphanumeric() -> None:
    from app.engine.live.account_identity import (
        InvalidAccountIdError,
        normalize_account_id,
    )

    with pytest.raises(InvalidAccountIdError):
        normalize_account_id("DU-1234")
    with pytest.raises(InvalidAccountIdError):
        normalize_account_id("DU/1234")


def test_normalize_rejects_empty_and_missing() -> None:
    from app.engine.live.account_identity import (
        InvalidAccountIdError,
        normalize_account_id,
    )

    with pytest.raises(InvalidAccountIdError, match="empty"):
        normalize_account_id("")
    with pytest.raises(InvalidAccountIdError, match="empty"):
        normalize_account_id("   ")


def test_normalize_requires_leading_letter() -> None:
    """The PRD's regex ``^[A-Z][A-Z0-9]+$`` requires a leading alpha. ``123ABC``
    fails so a bad-pattern wrong account never collides with a paper sentinel
    by accident."""
    from app.engine.live.account_identity import (
        InvalidAccountIdError,
        normalize_account_id,
    )

    with pytest.raises(InvalidAccountIdError):
        normalize_account_id("1234567")


def test_verify_match_passes_after_normalization() -> None:
    """Case difference and surrounding whitespace must not flag a mismatch."""
    from app.engine.live.account_identity import verify_account_match

    verify_account_match(
        ledger_account_id="  du1234567 ",
        connected_account="DU1234567",
    )


def test_verify_match_rejects_different_accounts() -> None:
    """The smoking-gun case — operator typed ``DU1234567`` at deploy but the
    Gateway bound to ``DU9999999``. The error must carry both raw values so the
    forensic record is unambiguous."""
    from app.engine.live.account_identity import (
        AccountIdentityMismatchError,
        verify_account_match,
    )

    with pytest.raises(AccountIdentityMismatchError) as exc:
        verify_account_match(
            ledger_account_id="DU1234567",
            connected_account="DU9999999",
        )
    msg = str(exc.value)
    assert "DU1234567" in msg
    assert "DU9999999" in msg


def test_verify_match_no_prefix_or_substring_shortcut() -> None:
    """No ``startswith("DU")`` heuristic. Two ``DU`` accounts that differ by
    more than a prefix still fail."""
    from app.engine.live.account_identity import (
        AccountIdentityMismatchError,
        verify_account_match,
    )

    with pytest.raises(AccountIdentityMismatchError):
        verify_account_match(
            ledger_account_id="DU111",
            connected_account="DU1112",  # not a prefix match
        )


def test_verify_match_rejects_malformed_ledger_account() -> None:
    """Pre-policy / corrupt ledger with malformed ``account_id``: refuse
    rather than silently fail-open. The error names the ledger value."""
    from app.engine.live.account_identity import (
        InvalidAccountIdError,
        verify_account_match,
    )

    with pytest.raises(InvalidAccountIdError):
        verify_account_match(
            ledger_account_id="",
            connected_account="DU1234567",
        )


def test_verify_match_rejects_malformed_broker_account() -> None:
    """If the broker's reported account is empty/malformed, also refuse —
    same error class. The fail-closed surface keeps the operator honest."""
    from app.engine.live.account_identity import (
        InvalidAccountIdError,
        verify_account_match,
    )

    with pytest.raises(InvalidAccountIdError):
        verify_account_match(
            ledger_account_id="DU1234567",
            connected_account=None,
        )
