"""Phase 3 / VCR-0006 — ledger↔broker account identity helpers.

The pure helpers are deliberately separate from ``LiveEngine`` so the
normalization and comparison rules are testable in isolation. The engine
integration is exercised in ``test_live_engine_halt.py``.
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


# ────────── LiveEngine integration — strict identity at start ────────


class _AccountCheckClient:
    """Minimal stub satisfying ``_validate_paper_client``'s interface.

    Mirrors only the attributes the validator reads: ``settings.mode``,
    ``settings.port``, and ``connected_account``. Real ``IbkrClient`` is
    significantly heavier; the validator is structurally simple enough that
    this stub is the right boundary for an integration test.
    """

    def __init__(self, connected_account: str) -> None:
        from app.broker.ibkr.config import IbkrSettings

        # Use a real ``IbkrSettings`` instance so PAPER_PORTS membership is
        # honoured without test-side reinvention; the only test-injected
        # value is ``connected_account``.
        self.settings = IbkrSettings(mode="paper", port=7497, host="127.0.0.1")
        self.connected_account = connected_account


def test_live_engine_refuses_to_start_on_account_mismatch(tmp_path) -> None:
    """The integration test: the engine's start gate raises the typed
    identity-mismatch error when the ledger says one ``DU*`` account and
    the broker is bound to a different ``DU*`` account. The check fires
    BEFORE strategy initialization (the very first line of ``run()``)."""
    from app.engine.live.account_identity import AccountIdentityMismatchError
    from app.engine.live.config import LiveConfig
    from app.engine.live.live_engine import LiveEngine
    from tests.engine.live.fixtures.fake_broker import FakeBroker

    broker = FakeBroker()
    engine = LiveEngine(
        _AccountCheckClient(connected_account="DU9999999"),
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU1234567",
    )

    with pytest.raises(AccountIdentityMismatchError) as exc:
        engine._validate_paper_client()
    assert "DU1234567" in str(exc.value)
    assert "DU9999999" in str(exc.value)


def test_live_engine_writes_session_metadata_on_match(tmp_path) -> None:
    """After the identity check passes, the engine persists the verified
    pair so a later audit can reconstruct the run's bound account."""
    from app.engine.live.config import LiveConfig
    from app.engine.live.live_engine import LiveEngine
    from app.engine.live.session_metadata import read_session_metadata
    from tests.engine.live.fixtures.fake_broker import FakeBroker

    broker = FakeBroker()
    engine = LiveEngine(
        _AccountCheckClient(connected_account="DU1234567"),
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU1234567",
        session_start_ms=1_780_000_000_000,
    )

    # Drive only the start-time gate: ``_validate_paper_client`` raises on
    # mismatch (already covered above); ``_write_session_metadata_on_start``
    # persists the sidecar after the check passes.
    engine._validate_paper_client()
    engine._write_session_metadata_on_start()

    metadata = read_session_metadata(tmp_path)
    assert metadata is not None
    assert metadata.ledger_account_id == "DU1234567"
    assert metadata.connected_account == "DU1234567"
    assert metadata.connection_epoch == 1


def test_live_engine_increments_connection_epoch_on_subsequent_session(tmp_path) -> None:
    """A second session in the same run_dir bumps ``connection_epoch`` so
    the forensic record distinguishes the original connect from the next."""
    from app.engine.live.config import LiveConfig
    from app.engine.live.live_engine import LiveEngine
    from app.engine.live.session_metadata import read_session_metadata
    from tests.engine.live.fixtures.fake_broker import FakeBroker

    for expected_epoch in (1, 2, 3):
        broker = FakeBroker()
        engine = LiveEngine(
            _AccountCheckClient(connected_account="DU1234567"),
            LiveConfig(),
            broker=broker,
            output_dir=tmp_path,
            account_id="DU1234567",
            session_start_ms=1_780_000_000_000 + expected_epoch,
        )
        engine._validate_paper_client()
        engine._write_session_metadata_on_start()
        metadata = read_session_metadata(tmp_path)
        assert metadata is not None
        assert metadata.connection_epoch == expected_epoch
