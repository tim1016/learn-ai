"""ADR 0048 Decision 4f — the fenced single-writer admission claim.

Collapses the four account-safety admission marker classes (``gate``,
``writer``, ``readers/*``, ``participants/*``) into one liveness-bound,
generation-fenced claim. The non-negotiable this module exists to prove: a
writer paused across a claim break (stopped-world GC, SIGSTOP, a suspended
VM — see T7, docs/audits/bot-fleet-stress-2026-08-26.md) must not be able to
complete a protected mutation on resume, because the store rejects its
stale generation, not because the writer remembered to check.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.engine.live.account_safety_admission_claim import (
    AccountSafetyAdmissionClaimLost,
    AccountSafetyAdmissionError,
    account_safety_admission_claim,
    open_write_transaction,
    persist_protected_payload_on_connection,
    read_protected_payload,
    try_acquire_or_break_claim,
    validate_claim_on_connection,
)

ACCOUNT_ID = "DU1234567"


def test_paused_owner_with_broken_claim_cannot_complete_protected_mutation(tmp_path: Path) -> None:
    """The whole point of ADR 0048 4f.

    Owner A acquires the claim, then is "paused" (a stopped-world GC, a
    SIGSTOP, a suspended VM) — it keeps holding its exact (owner, generation)
    handle, unaware time has moved on. Owner B breaks the now-orphaned claim.
    A resumes and tries to validate the *stale* generation it is still
    holding, immediately before it would perform its protected mutation.
    The store must reject it — not because A checked its own memory (a check
    a paused writer always passes) but because the store's own compare-and
    -swap no longer matches.
    """

    claim_a = try_acquire_or_break_claim(tmp_path, ACCOUNT_ID, owner="boot:aaaa:pid:1", now_ms=0, ttl_ms=1_000)
    assert claim_a is not None
    assert claim_a.generation == 1

    # A is paused here, indefinitely, holding `claim_a` unchanged.

    # Time moves past A's TTL and B breaks the orphaned claim -- no operator
    # action, no repair ceremony (ADR 0048 4d).
    claim_b = try_acquire_or_break_claim(tmp_path, ACCOUNT_ID, owner="boot:bbbb:pid:2", now_ms=5_000, ttl_ms=1_000)
    assert claim_b is not None
    assert claim_b.generation == 2

    # A resumes and, still believing it holds generation 1, attempts to
    # validate immediately before its protected write.
    with pytest.raises(AccountSafetyAdmissionClaimLost):
        claim_a.validate_or_raise(now_ms=5_500)

    # B's own current claim still validates fine.
    claim_b.validate_or_raise(now_ms=5_500)


def test_claim_break_cannot_interleave_with_a_paused_validated_write(tmp_path: Path) -> None:
    """ADR 0048 4f.3: the CAS validate and the durable write it guards must
    be ONE atomic operation against the store -- not a check that succeeds,
    then a *separate* write with no re-check. A version that validates via
    this claim's SQLite store and then persists via something else entirely
    (the previously committed design: ``validate_or_raise()`` followed by a
    plain ``os.replace`` on a JSON file) has exactly this gap: nothing
    prevents a break from landing in between.

    Proven with a REAL pause, not a mock or a sleep: thread A validates its
    claim successfully and then holds its transaction open (simulating a
    stopped-world GC / SIGSTOP landing between the check and the write),
    while thread B makes a real, concurrent attempt to break the same
    claim. If validate and persist are genuinely one transaction, B's
    break must BLOCK on SQLite's own write lock for as long as A's
    transaction is open -- there is no instant at which A is "validated but
    not yet durable" that B can exploit. Once A's transaction resolves, B's
    blocked attempt proceeds and the account remains breakable (ADR 0048
    4d) -- a paused writer does not brick the claim forever, it only holds
    it for the (narrow, I/O-free) span of its own atomic write.
    """

    claim_a = try_acquire_or_break_claim(tmp_path, ACCOUNT_ID, owner="A", now_ms=0, ttl_ms=100_000)
    assert claim_a is not None

    validated = threading.Event()
    resume = threading.Event()
    write_outcome: dict[str, object] = {}

    def paused_writer() -> None:
        conn = open_write_transaction(tmp_path, ACCOUNT_ID)
        try:
            validate_claim_on_connection(conn, claim_a, now_ms=1)
            validated.set()
            # "Paused" here -- exactly the window ADR 0048 4f.3 forbids --
            # with the transaction still open, before the durable write.
            assert resume.wait(timeout=1)
            persist_protected_payload_on_connection(conn, payload_json='{"stale": true}', updated_at_ms=2)
            conn.execute("COMMIT")
            write_outcome["committed"] = True
        except Exception:
            conn.execute("ROLLBACK")
            write_outcome["committed"] = False
            raise
        finally:
            conn.close()

    writer = threading.Thread(target=paused_writer)
    writer.start()
    assert validated.wait(timeout=1)

    break_outcome: dict[str, object] = {}

    def attempt_break() -> None:
        break_outcome["claim"] = try_acquire_or_break_claim(
            tmp_path, ACCOUNT_ID, owner="B", now_ms=999_999, ttl_ms=100_000
        )

    breaker = threading.Thread(target=attempt_break)
    breaker.start()
    breaker.join(timeout=0.3)

    # THE ASSERTION: while A's validated transaction is still open ("paused"),
    # B must not have been able to break the claim yet -- this is RED
    # against a design where validate and the durable write are two
    # independent operations, because nothing there holds any lock across
    # A's "pause."
    assert breaker.is_alive(), (
        "B broke the claim while A's validated write was still in flight -- "
        "the check and the durable write are not atomic"
    )

    resume.set()
    writer.join(timeout=1)
    breaker.join(timeout=1)

    assert write_outcome["committed"] is True
    assert break_outcome["claim"] is not None
    assert break_outcome["claim"].generation == 2
    assert read_protected_payload(tmp_path, ACCOUNT_ID) == '{"stale": true}'


def test_generation_increments_monotonically_across_breaks(tmp_path: Path) -> None:
    claim1 = try_acquire_or_break_claim(tmp_path, ACCOUNT_ID, owner="A", now_ms=0, ttl_ms=100)
    assert claim1 is not None
    assert claim1.generation == 1

    # B cannot acquire while A's claim is still live.
    contended = try_acquire_or_break_claim(tmp_path, ACCOUNT_ID, owner="B", now_ms=50, ttl_ms=100)
    assert contended is None

    # After A's claim expires, B breaks it -- generation bumps to 2.
    claim2 = try_acquire_or_break_claim(tmp_path, ACCOUNT_ID, owner="B", now_ms=150, ttl_ms=100)
    assert claim2 is not None
    assert claim2.generation == 2

    # A's superseded generation is rejected by the store.
    with pytest.raises(AccountSafetyAdmissionClaimLost):
        claim1.validate_or_raise(now_ms=150)

    # C breaks B's claim after it too expires -- generation bumps to 3.
    claim3 = try_acquire_or_break_claim(tmp_path, ACCOUNT_ID, owner="C", now_ms=300, ttl_ms=100)
    assert claim3 is not None
    assert claim3.generation == 3

    # B's now-superseded generation is rejected too.
    with pytest.raises(AccountSafetyAdmissionClaimLost):
        claim2.validate_or_raise(now_ms=300)

    # C's current claim validates.
    claim3.validate_or_raise(now_ms=300)


def test_orphaned_claim_is_breakable_without_operator_action(tmp_path: Path) -> None:
    """The gate/writer orphan class (ADR 0048 4e) -- the one that caused the
    real S4 outage, judged at 10.0s acquisition, not the harmless 0.00s
    participant roster. Must be breakable by ordinary acquisition: no
    operator marker deletion, no repair ceremony.
    """

    orphan = try_acquire_or_break_claim(tmp_path, ACCOUNT_ID, owner="dead-owner", now_ms=0, ttl_ms=1_000)
    assert orphan is not None

    # `orphan`'s process crashes/pauses forever and never releases.  No
    # operator action happens here -- no marker file deleted, no repair
    # ceremony invoked.  Once the claim has aged past its TTL, an ordinary
    # acquisition attempt must simply succeed.
    revived = try_acquire_or_break_claim(tmp_path, ACCOUNT_ID, owner="new-owner", now_ms=5_000, ttl_ms=1_000)
    assert revived is not None
    assert revived.owner == "new-owner"
    assert revived.generation == orphan.generation + 1


def test_admission_claim_context_manager_acquires_and_releases(tmp_path: Path) -> None:
    with account_safety_admission_claim(tmp_path, ACCOUNT_ID, owner="A", now_ms=0, ttl_ms=1_000) as claim:
        assert claim.owner == "A"
        assert claim.generation == 1
        claim.validate_or_raise(now_ms=0)

    # After a clean release, a fresh acquisition is uncontended.
    with account_safety_admission_claim(tmp_path, ACCOUNT_ID, owner="B", now_ms=1, ttl_ms=1_000) as claim:
        assert claim.owner == "B"
        assert claim.generation == 2


def test_admission_claim_context_manager_raises_when_still_held(tmp_path: Path) -> None:
    held = try_acquire_or_break_claim(tmp_path, ACCOUNT_ID, owner="A", now_ms=0, ttl_ms=10_000)
    assert held is not None

    with (
        pytest.raises(AccountSafetyAdmissionError),
        account_safety_admission_claim(tmp_path, ACCOUNT_ID, owner="B", now_ms=0, acquire_timeout_s=0.05),
    ):
        pass
