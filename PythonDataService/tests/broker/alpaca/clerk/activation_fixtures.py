"""The activation-store double the authority-selection tests share (ADR 0059 D1).

Not a conftest: imported by name from ``tests/broker/alpaca/clerk/`` and
``tests/services/``, which share no conftest — the ``live_arming_fixtures``
precedent. It lives here rather than inside one ``test_*.py`` module so that
importing it does not make a test module an implicit shared library.
"""

from __future__ import annotations

from pathlib import Path

from app.broker.alpaca.clerk.sqlite.activation import ActivationRecordInvalid


class _ActivationStore:
    """One in-memory activation record, with the two refusals a real store can raise."""

    def __init__(
        self,
        record: object | None,
        *,
        invalid: bool = False,
        resolve_invalid: bool = False,
    ) -> None:
        self.record = record
        self.invalid = invalid
        self.resolve_invalid = resolve_invalid
        self.resolved: tuple[str, int, str, Path] | None = None

    def latest(self, _account_id: str) -> object | None:
        if self.invalid:
            raise ActivationRecordInvalid("tampered activation")
        return self.record

    def resolve(
        self,
        account_id: str,
        authority_generation: int,
        db_identity_token: str,
        artifacts_root: Path,
    ) -> object:
        if self.resolve_invalid:
            raise ActivationRecordInvalid("activation does not match SQLite identity")
        self.resolved = (
            account_id,
            authority_generation,
            db_identity_token,
            artifacts_root,
        )
        assert self.record is not None
        return self.record


__all__ = ["_ActivationStore"]
