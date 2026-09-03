"""The per-run continuity policy a bot hands its feed (#1921).

The policy is the bot's half of the reconnect contract: which minutes it
decides on, what it will authorize the feed to substitute, and where the
feed's continuity facts are recorded. A binding the policy cannot describe
truthfully gets no policy at all -- the feed then behaves exactly as it did
before #1921 rather than being handed a clock it cannot honor.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.marketdata.feed import FeedContinuityEvent, SubstitutionRefusal
from app.services import feed_continuity_policy as module
from app.services.feed_continuity_policy import continuity_policy_for
from app.services.source_bar_ledger import SourceBarLedger
from tests.services.test_signal_program_admission import _sealed_binding as _sealed_rth_binding

# 15:00 ET on 2026-09-02, the close of a 15-minute decision bucket.
_BUCKET_CLOSE_MS = 1_788_375_600_000


def test_continuity_policy_for_unsealed_binding_gets_no_policy(tmp_path: Path) -> None:
    binding = _sealed_rth_binding().model_copy(update={"sealed_program": None})
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        assert continuity_policy_for(binding, ledger) is None
    finally:
        ledger.close()


def test_continuity_policy_for_all_session_binding_gets_no_policy(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Ruling R1: only the calendar-proven regular session has a trigger set.

    An extended-hours binding is refused a policy rather than given an RTH
    clock that would call its overnight minutes undecidable.
    """
    binding = _sealed_rth_binding().model_copy(update={"use_rth": False})
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        with caplog.at_level(logging.INFO, logger=module.__name__):
            assert continuity_policy_for(binding, ledger) is None
    finally:
        ledger.close()

    declined = [record for record in caplog.records if getattr(record, "action", None) == "feed_continuity_not_offered"]
    assert [record.reason for record in declined] == ["all_session_not_supported"]


def test_continuity_policy_for_binding_without_a_decision_timeframe_gets_no_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed if a seal ever stops attesting a decision clock.

    Today every seal carries ``decision_timeframe_ms``, so this branch is
    reachable only by removing it; the point of the test is that the width is
    read, never assumed, so a seal shape that drops it declines a policy
    instead of scheduling against a guessed timeframe.
    """
    binding = _sealed_rth_binding()
    monkeypatch.setattr(module, "decision_timeframe_ms_for_binding", lambda _binding: None)
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        assert continuity_policy_for(binding, ledger) is None
    finally:
        ledger.close()


async def test_continuity_policy_for_sealed_rth_binding_refuses_substitution_and_sinks_to_the_run(
    tmp_path: Path,
) -> None:
    binding = _sealed_rth_binding()
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="acct")
    try:
        policy = continuity_policy_for(binding, ledger)

        assert policy is not None and policy.decision_session == "rth"
        assert policy.substitution_grant(0, 60_000) == SubstitutionRefusal(reason="SUBSTITUTION_NOT_AUTHORIZED")
        ref = await policy.record_event(
            FeedContinuityEvent(kind="interruption", feed_id="ibkr", symbol="SPY", observed_at_ms=1)
        )
        assert ref.run_id == binding.run_id
        assert [event.kind for event in ledger.events(run_id=binding.run_id)] == ["interruption"]
        # The seal declares a 15-minute decision clock, so the bucket closing
        # at 15:00 ET triggers on the first source minute of the next bucket.
        assert policy.next_trigger_ms(_BUCKET_CLOSE_MS) == _BUCKET_CLOSE_MS + 60_000
    finally:
        ledger.close()
