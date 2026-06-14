"""VCR-0006 / Phase 3 — strict ledger↔broker account identity helpers.

The run ledger hashes ``account_id`` into ``run_id`` as one of the seven
identity fields. Until Phase 3 the engine validated only the paper sentinel
(``DU`` prefix) and never compared the operator-typed
``ledger.account_id`` against the broker-reported ``connected_account``. A
misconfigured ``IBKR_HOST`` / ``client_id`` could route orders to a different
``DU*`` account than the operator typed; every downstream artifact then
attested the wrong account.

This module owns the pure logic — normalization and comparison — so the rules
are testable in isolation. The engine integration lives in
``LiveEngine._validate_paper_client``.

Final contract (per PRD §11):

- ``normalize(raw) := raw.strip().upper()``, must match ``^[A-Z][A-Z0-9]+$``.
- Comparison: ``normalize(ledger) == normalize(broker)`` — no prefix-match,
  substring-match, or ``startswith("DU")`` shortcut.
- Empty / missing / malformed identity is **view-only / redeploy-only**.
  Mirrors Phase 1's "missing sizing = view-only/redeploy-only" policy:
  ``account_id`` is hashed into ``run_id``, so any in-place mutation would
  make the identity fingerprint dishonest.
"""

from __future__ import annotations

import re

_ACCOUNT_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9]+$")


class InvalidAccountIdError(ValueError):
    """A raw account_id cannot be normalized to the canonical form.

    Subclass of ``ValueError`` so existing code that catches ``ValueError``
    keeps working. Callers in the engine map it to a fatal-halt with the
    raw value preserved for forensic reconstruction.
    """


class AccountIdentityMismatchError(RuntimeError):
    """The ledger's ``account_id`` does not match the broker's
    ``connected_account`` after normalization.

    Carries both raw values so the operator's next step is unambiguous.
    Distinct from the future ``BROKER_SAFETY_VERDICT_TRANSITION_HALT``:
    that one fires when the verdict (mode/port/readonly/prefix) degrades;
    this one fires when the broker account identity itself changed.
    """

    def __init__(self, *, ledger_account_id: str, connected_account: str | None) -> None:
        self.ledger_account_id = ledger_account_id
        self.connected_account = connected_account
        super().__init__(
            f"Ledger account_id ({ledger_account_id!r}) does not match "
            f"broker-reported connected_account ({connected_account!r}). "
            "Check IBKR_HOST / client_id / Gateway account selection — the "
            "engine refuses to start when the deploy-time identity disagrees "
            "with the runtime-bound identity (VCR-0006 / Phase 3)."
        )


def normalize_account_id(raw: str | None) -> str:
    """Normalize and validate a raw account_id to its canonical form.

    Strip surrounding whitespace, uppercase, and require
    ``^[A-Z][A-Z0-9]+$``. Raise :class:`InvalidAccountIdError` for any input
    that cannot be normalized — empty, whitespace-only, internal whitespace,
    non-alphanumeric characters, or a leading digit.
    """
    if raw is None:
        raise InvalidAccountIdError("account_id is empty (got None)")
    if not isinstance(raw, str):
        raise InvalidAccountIdError(
            f"account_id must be a string, got {type(raw).__name__}"
        )
    stripped = raw.strip()
    if not stripped:
        raise InvalidAccountIdError("account_id is empty after stripping whitespace")
    if any(ch.isspace() for ch in stripped):
        raise InvalidAccountIdError(
            f"account_id contains internal whitespace: {raw!r}"
        )
    canonical = stripped.upper()
    if not _ACCOUNT_ID_PATTERN.fullmatch(canonical):
        raise InvalidAccountIdError(
            f"account_id {raw!r} does not match pattern ^[A-Z][A-Z0-9]+$ "
            f"(after normalization: {canonical!r})"
        )
    return canonical


def verify_account_match(
    *,
    ledger_account_id: str | None,
    connected_account: str | None,
) -> None:
    """Refuse to proceed if the ledger and broker accounts do not match.

    Raises :class:`InvalidAccountIdError` if either value fails to
    normalize, :class:`AccountIdentityMismatchError` if both are well-formed
    but disagree. On match, returns ``None``.

    Carries both raw values into the error so the operator's failure list
    surfaces the deploy-time identity and the runtime-bound identity side
    by side.
    """
    try:
        ledger_canonical = normalize_account_id(ledger_account_id)
    except InvalidAccountIdError as exc:
        raise InvalidAccountIdError(
            f"ledger.account_id is invalid: {exc}"
        ) from exc
    try:
        broker_canonical = normalize_account_id(connected_account)
    except InvalidAccountIdError as exc:
        raise InvalidAccountIdError(
            f"broker.connected_account is invalid: {exc}"
        ) from exc
    if ledger_canonical != broker_canonical:
        raise AccountIdentityMismatchError(
            ledger_account_id=str(ledger_account_id),
            connected_account=str(connected_account),
        )
