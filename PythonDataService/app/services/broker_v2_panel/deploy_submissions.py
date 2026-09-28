"""Backend-authored bot names and the Deploy submission ledger (#2551, PRD #2560).

A bot is named when its Deploy is first claimed, never by the client:
``<symbol>-<strategy code>-<YYYYMMDD>-<HHMM>``, lowercase, with the minute read
on the New York exchange clock. A second bot claimed for the same symbol,
strategy and minute is ``…-2``, then ``…-3``.

The client sends an opaque per-submission key instead of a name. The ledger
maps that key to the one name it was given, so a double-click, a reload or a
lost response returns the same bot, while a second submission with identical
settings (a new key) is a second bot.

Allocation is fenced twice. One process-wide lock serializes every claim in
this clerk, and every record is published create-once (``link(2)`` fails if
the name exists), so two submissions -- concurrent in one process, or in two
processes sharing the volume -- can never hold one name or one key. The name
is claimed before the Deploy's custody commit because everything the commit
writes is keyed by it (the runner binding, the Dry Run's private ``sim:``
authority); the commit then fails closed on its own identity fences if the
name were ever reused.

The first-deploy instant is recorded beside the name as its own ``int64 ms
UTC`` field. Nothing parses a name back into a time, and no existing bot is
ever renamed: a legacy id simply has no ledger record.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime
from app.broker.alpaca.clerk.models import EpochMs
from app.engine.live.durable_append_log import create_atomic_exclusive_durable_file
from app.engine.live.identity import strategy_instance_artifact_dir, validate_strategy_instance_id
from app.engine.live.order_identity import (
    InstanceIdTooLongError,
    validate_broker_owned_instance_id,
)
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.utils.timestamps import ny_datetime

#: An opaque key the browser mints when the Deploy form opens. It is a file
#: name here, so it is held to a path-safe shape.
SUBMISSION_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$"
_SUBMISSION_KEY_RE = re.compile(SUBMISSION_KEY_PATTERN)

#: Same-minute bots of one symbol and strategy beyond this are refused
#: rather than named past the broker-owned length cap.
_MAX_ORDINAL = 99

#: One claim at a time in this process; see the module docstring.
_CLAIM_LOCK = threading.Lock()


class BotNameUnavailable(ValueError):
    """No valid bot name can be authored for this Deploy."""


class DeploySubmissionConflict(ValueError):
    """A submission key was sent again with different settings."""


class DeploySubmission(BaseModel):
    """The one bot a Deploy submission was given, recorded create-once."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    submission_key: str = Field(pattern=SUBMISSION_KEY_PATTERN)
    strategy_instance_id: str = Field(min_length=1, max_length=128)
    # The instant the Deploy was first claimed: the canonical first-deploy
    # time, and the instant the name's New York minute was read from.
    first_deployed_at_ms: EpochMs
    # Content only (``AlpacaPaperDeployRequest.content``): the settings the
    # key was first sent with, so a resend with other settings is refused.
    request_fingerprint: str = Field(min_length=1)
    # Display-only lineage from Deploy again. It grants nothing.
    replaces_strategy_instance_id: str | None = Field(default=None, max_length=128)


def bot_name_prefix(symbol: str, strategy_key: str) -> str:
    """``<symbol>-<strategy code>``, lowercase: every name's fixed part."""
    registration = _STRATEGY_REGISTRY.get(strategy_key)
    if registration is None or not registration.deploy_code:
        raise BotNameUnavailable(f"Strategy {strategy_key!r} has no bot-name code.")
    return f"{symbol.lower()}-{registration.deploy_code}"


def bot_name(symbol: str, strategy_key: str, *, at_ms: int, ordinal: int = 1) -> str:
    """The ``ordinal``-th bot name for this symbol and strategy in ``at_ms``'s NY minute.

    The minute is read through ``America/New_York`` (DST-correct), never a
    fixed offset. The result satisfies the path-safe instance-id pattern and
    the broker-owned length cap every order reference is built within.
    """
    minute = ny_datetime(at_ms)
    name = f"{bot_name_prefix(symbol, strategy_key)}-{minute:%Y%m%d}-{minute:%H%M}"
    if ordinal > 1:
        name = f"{name}-{ordinal}"
    try:
        return validate_broker_owned_instance_id(validate_strategy_instance_id(name))
    except InstanceIdTooLongError as exc:
        raise BotNameUnavailable(
            f"A bot for {symbol} cannot be named within the broker's order-reference limit ({name!r} is too long)."
        ) from exc


def bot_name_note(symbol: str, strategy_key: str) -> str:
    """How the bot will be named, for the Deploy preview's review."""
    return (
        f"Named at Deploy: {bot_name_prefix(symbol, strategy_key)}-YYYYMMDD-HHMM, "
        "from the New York minute the Deploy is committed."
    )


def require_submission_key(value: str) -> str:
    if _SUBMISSION_KEY_RE.fullmatch(value) is None:
        raise ValueError("submission_key must be 8-64 letters, digits, '-' or '_'")
    return value


def _bot_exists(artifacts_root: Path, strategy_instance_id: str) -> bool:
    """Whether any bot -- including one named by hand before #2551 -- holds this id."""
    if strategy_instance_artifact_dir(artifacts_root, "live_state", strategy_instance_id).exists():
        return True
    runtime = get_active_clerk_runtime()
    repo = None if runtime is None else runtime.sqlite_repository
    return repo is not None and repo.strategy_instance(strategy_instance_id) is not None


class DeploySubmissionLedger:
    """Create-once ``key -> name`` and ``name -> submission`` records on the lane's volume.

    Lives beside the lane's bot bindings (``<artifacts_root>/deploy_submissions``)
    so it answers for exactly the bots this clerk deploys.
    """

    def __init__(self, artifacts_root: Path, *, name_in_use: Callable[[str], bool] | None = None) -> None:
        self._root = Path(artifacts_root) / "deploy_submissions"
        self._name_in_use = name_in_use or (lambda sid: _bot_exists(Path(artifacts_root), sid))

    def by_key(self, submission_key: str) -> DeploySubmission | None:
        return self._read(self._key_path(submission_key))

    def by_name(self, strategy_instance_id: str) -> DeploySubmission | None:
        return self._read(self._name_path(strategy_instance_id))

    def provisional_name(self, *, symbol: str, strategy_key: str, now_ms: int) -> str:
        """The name a Deploy claimed now would get, reserving nothing.

        Admission previews run the exact Start checks against it, so they
        describe the bot the Deploy would create.
        """
        return next(self._free_names(symbol=symbol, strategy_key=strategy_key, now_ms=now_ms))

    def claim(
        self,
        *,
        submission_key: str,
        symbol: str,
        strategy_key: str,
        request_fingerprint: str,
        replaces_strategy_instance_id: str | None,
        now_ms: int,
    ) -> DeploySubmission:
        """Return this key's bot, naming it now if the key is new.

        A key already claimed returns its recorded bot unchanged when its
        settings match, and is refused when they do not.
        """
        with _CLAIM_LOCK:
            existing = self.by_key(submission_key)
            if existing is not None:
                return _same_settings(existing, request_fingerprint)
            for name in self._free_names(symbol=symbol, strategy_key=strategy_key, now_ms=now_ms):
                record = DeploySubmission(
                    submission_key=submission_key, strategy_instance_id=name, first_deployed_at_ms=now_ms,
                    request_fingerprint=request_fingerprint,
                    replaces_strategy_instance_id=replaces_strategy_instance_id,
                )
                try:
                    self._publish(self._name_path(name), record)
                except FileExistsError:
                    # Another process reserved this name between the check and
                    # the create: the create is the fence, so take the next.
                    continue
                try:
                    self._publish(self._key_path(submission_key), record)
                except FileExistsError:
                    # Another process claimed this key first. Its bot stands;
                    # the name reserved above is abandoned, never reused.
                    winner = self.by_key(submission_key)
                    assert winner is not None
                    return _same_settings(winner, request_fingerprint)
                return record
        raise BotNameUnavailable(
            f"More than {_MAX_ORDINAL} {symbol} bots of this strategy were deployed this minute. Try again in a minute."
        )

    def _free_names(self, *, symbol: str, strategy_key: str, now_ms: int):
        for ordinal in range(1, _MAX_ORDINAL + 1):
            name = bot_name(symbol, strategy_key, at_ms=now_ms, ordinal=ordinal)
            if not self._name_path(name).exists() and not self._name_in_use(name):
                yield name

    def _key_path(self, submission_key: str) -> Path:
        return self._root / "keys" / f"{require_submission_key(submission_key)}.json"

    def _name_path(self, strategy_instance_id: str) -> Path:
        return self._root / "names" / f"{validate_strategy_instance_id(strategy_instance_id)}.json"

    def _publish(self, path: Path, record: DeploySubmission) -> None:
        serialized = json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        create_atomic_exclusive_durable_file(path, serialized, trusted_root=self._root)

    @staticmethod
    def _read(path: Path) -> DeploySubmission | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return DeploySubmission.model_validate_json(raw)


def _same_settings(existing: DeploySubmission, request_fingerprint: str) -> DeploySubmission:
    if existing.request_fingerprint != request_fingerprint:
        raise DeploySubmissionConflict(
            f"This Deploy was already sent with different settings as {existing.strategy_instance_id}."
        )
    return existing


__all__ = [
    "SUBMISSION_KEY_PATTERN",
    "BotNameUnavailable",
    "DeploySubmission",
    "DeploySubmissionConflict",
    "DeploySubmissionLedger",
    "bot_name",
    "bot_name_note",
    "bot_name_prefix",
    "require_submission_key",
]
