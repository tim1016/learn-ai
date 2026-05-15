"""Indicator state persistence — envelope, payload, policy, receipt, validation.

Generic envelope, strategy-specific payload. The envelope's identity
fields (strategy_key, symbol, consolidator_period_min) double as the
sidecar's lookup key on the filesystem.

The validation ladder (consumed by hydrate(), implemented in this
module in a later task) runs six checks in order; first failure stops
and populates ``ValidationResult.failure_reason``. See
``docs/superpowers/specs/2026-05-15-spy-ema-paper-dry-run-design.md``
§4.1 for the ladder definition.

Decimal-safe: indicator internal numeric state serializes as quoted
strings in the payload (passed through as ``dict[str, Any]`` from this
module's POV; per-strategy ``validate_state_payload`` enforces the
shape). int64 ms UTC for every timestamp at the wire boundary.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sys
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class HydratePolicy(StrEnum):
    """Tri-state policy for indicator-state hydration on start.

    Default for the B2 dry-run gate and paper-week operation is REQUIRE.
    OPTIONAL is for seed days. DISABLED is the explicit operator escape
    hatch (--allow-cold-start) that skips the read entirely but still
    writes at end-of-session so today seeds tomorrow.
    """

    REQUIRE = "require"
    OPTIONAL = "optional"
    DISABLED = "disabled"


FailureReason = Literal[
    "disabled_by_operator",
    "missing",
    "schema_mismatch",
    "identity_mismatch",
    "calendar_stale",
    "payload_mismatch",
    "indicators_unready",
    "lifecycle_not_flat",
]


class ValidationResult(BaseModel):
    """Per-check booleans + the first failure reason."""

    model_config = ConfigDict(frozen=True)

    schema_version_ok: bool = True
    identity_ok: bool = True
    calendar_ok: bool = True
    payload_shape_ok: bool = True
    indicators_ready_ok: bool = True
    lifecycle_flat_ok: bool = True
    failure_reason: FailureReason | None = None

    @classmethod
    def all_passed(cls) -> ValidationResult:
        return cls()

    @classmethod
    def failed(cls, reason: FailureReason, **flag_overrides: bool) -> ValidationResult:
        return cls(failure_reason=reason, **flag_overrides)


CapturedReason = Literal["force_flat", "shutdown"]


class IndicatorStateEnvelope(BaseModel):
    """Generic envelope wrapping a strategy-specific payload.

    Identity tuple = (strategy_key, symbol, consolidator_period_min).
    Used both as the sidecar's filesystem key and as the validation
    ladder's identity check (#2).

    Timestamps are int64 ms UTC. Payload is opaque to this module —
    each strategy is responsible for its own payload shape via
    ``validate_state_payload``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    strategy_key: str
    symbol: str
    consolidator_period_min: int = Field(gt=0)
    last_consolidated_bar_end_ms: int = Field(gt=0)
    captured_at_ms: int = Field(gt=0)
    captured_reason: CapturedReason
    code_sha: str
    strategy_spec_sha: str
    payload: dict[str, Any]


class HydrationReceipt(BaseModel):
    """Per-run forensic record of what happened at hydrate time.

    Always written, regardless of accepted=true/false. The reconcile
    hash manifest picks it up if present.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    hydrated_at_ms: int
    policy: HydratePolicy
    global_path: str
    global_sha256: str | None = None
    accepted: bool
    strategy_key: str
    symbol: str
    consolidator_period_min: int
    sidecar_last_consolidated_bar_end_ms: int | None = None
    expected_prev_session_close_ms: int | None = None
    calendar: str = "NYSE"
    validation: ValidationResult


class IndicatorStateHydrationError(RuntimeError):
    """Raised when hydrate() is called under REQUIRE policy and validation fails.

    Carries the receipt the runner just wrote so callers can surface
    the failure reason without re-reading the file.
    """

    def __init__(self, receipt: HydrationReceipt) -> None:
        self.receipt = receipt
        super().__init__(
            f"indicator state hydration failed under {receipt.policy.value} policy: {receipt.validation.failure_reason}"
        )


def stable_global_path(
    artifacts_root: Path,
    strategy_key: str,
    symbol: str,
    consolidator_period_min: int,
) -> Path:
    """Return the canonical sidecar path for an identity tuple.

    Layout: <artifacts_root>/live_state/<strategy_key>/<symbol>_<period>m.json
    """
    return artifacts_root / "live_state" / strategy_key / f"{symbol.upper()}_{consolidator_period_min}m.json"


class IndicatorStateRepo:
    """Atomic-write JSON repo for a single envelope on a stable path.

    Path identity = (strategy_key, symbol, consolidator_period_min);
    callers construct the path themselves from the identity tuple via
    ``stable_global_path``.

    Atomic write: serialize -> write to <path>.tmp -> os.replace.
    On POSIX this is atomic for the rename; on Windows os.replace
    handles the existing-file case too.

    Advisory lock: best-effort fcntl on POSIX, msvcrt on Windows.
    The lock window is the duration of the atomic write only.
    Concurrent readers may see either the old or the new file but
    never a torn one. (The runner is a single process; the lock
    guards against developer footguns like two CLI invocations
    racing on the same machine.)
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> IndicatorStateEnvelope | None:
        """Return the envelope, or None if the file does not exist.

        Raises on malformed JSON or schema violations — the validation
        ladder converts that into a ``schema_mismatch`` receipt.
        """
        if not self._path.exists():
            return None
        with self._path.open("r", encoding="utf-8") as fh:
            return IndicatorStateEnvelope.model_validate_json(fh.read())

    def write(self, envelope: IndicatorStateEnvelope) -> None:
        """Atomic write of envelope to ``self._path`` under advisory lock."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        payload_json = envelope.model_dump_json(indent=2).encode("utf-8")
        with _file_lock(self._path):
            with open(tmp_path, "wb") as fh:
                fh.write(payload_json)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._path)

    def is_strictly_newer_than_on_disk(self, candidate: IndicatorStateEnvelope) -> bool:
        """Return True iff there is no existing sidecar or candidate's bar is strictly newer."""
        if not self._path.exists():
            return True
        existing = self.read()
        if existing is None:
            return True
        return candidate.last_consolidated_bar_end_ms > existing.last_consolidated_bar_end_ms

    def sha256_of_on_disk(self) -> str | None:
        """Return SHA-256 hex of the on-disk bytes, or None if absent."""
        if not self._path.exists():
            return None
        h = hashlib.sha256()
        with self._path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()


@contextlib.contextmanager
def _file_lock(target_path: Path):  # type: ignore[return]
    """Acquire an advisory lock on a sibling .lock file for the lifetime of the context.

    Best-effort. Failure to acquire (lock-file directory denied,
    Windows lock-file already open from another process) raises so
    the caller surfaces it rather than silently writing without
    synchronization.
    """
    lock_path = target_path.with_suffix(target_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")  # noqa: SIM115
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()
