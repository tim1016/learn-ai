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
import time as _time
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
            try:
                os.replace(tmp_path, self._path)
            except Exception:
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
                raise

    def is_strictly_newer_than_on_disk(self, candidate: IndicatorStateEnvelope) -> bool:
        """Return True iff there is no existing sidecar or candidate's bar is strictly newer."""
        if not self._path.exists():
            return True
        existing = self.read()
        if existing is None:
            return True
        return candidate.last_consolidated_bar_end_ms > existing.last_consolidated_bar_end_ms

    def write_if_strictly_newer(self, candidate: IndicatorStateEnvelope) -> bool:
        """Atomic compare-and-write: write only if candidate is strictly newer than on-disk.

        Returns True if the write happened, False if it was skipped because
        on-disk envelope is equal or newer. The compare and write are under
        the same advisory lock — no race window between the check and the
        replace.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        payload_json = candidate.model_dump_json(indent=2).encode("utf-8")

        with _file_lock(self._path):
            if self._path.exists():
                with self._path.open("r", encoding="utf-8") as fh:
                    existing = IndicatorStateEnvelope.model_validate_json(fh.read())
                if candidate.last_consolidated_bar_end_ms <= existing.last_consolidated_bar_end_ms:
                    return False
            with open(tmp_path, "wb") as fh:
                fh.write(payload_json)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.replace(tmp_path, self._path)
            except Exception:
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
                raise
            return True

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
    fh.seek(0)  # msvcrt.locking is byte-range; ensure lock and unlock target offset 0
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


def hydrate(
    *,
    strategy: Any,
    policy: HydratePolicy,
    artifacts_root: Path,
    run_dir: Path,
    session_start_ms: int,
) -> None:
    """Run the six-row validation ladder + write the hydration receipt.

    Side effects:
      * Writes <run_dir>/indicator_state_hydration.json (always).
      * Calls strategy.restore_state_from_persistence(payload) on success.
      * Raises IndicatorStateHydrationError if policy=REQUIRE and the
        ladder rejects (after writing the receipt).
    """
    # Local import to keep module-load surface minimal.
    from app.lean_sidecar.trading_calendar import NoSessionError, previous_completed_session_close_ms

    strategy_key: str = strategy.STRATEGY_KEY
    symbol: str = (
        strategy.ctx.symbols[0]
        if strategy.ctx is not None and strategy.ctx.symbols
        else getattr(strategy, "_symbol_name", "")
    )
    period: int = strategy.CONSOLIDATOR_PERIOD_MIN
    receipt_path = run_dir / "indicator_state_hydration.json"
    global_path = stable_global_path(artifacts_root, strategy_key, symbol, period)
    repo = IndicatorStateRepo(global_path)

    def _write_receipt(receipt: HydrationReceipt) -> None:
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(receipt.model_dump_json(indent=2))

    def _base_receipt(
        accepted: bool,
        validation: ValidationResult,
        sidecar_last_bar_ms: int | None = None,
        expected_prev_close_ms: int | None = None,
        global_sha: str | None = None,
    ) -> HydrationReceipt:
        return HydrationReceipt(
            schema_version=1,
            hydrated_at_ms=int(_time.time() * 1000),
            policy=policy,
            global_path=str(global_path),
            global_sha256=global_sha,
            accepted=accepted,
            strategy_key=strategy_key,
            symbol=symbol,
            consolidator_period_min=period,
            sidecar_last_consolidated_bar_end_ms=sidecar_last_bar_ms,
            expected_prev_session_close_ms=expected_prev_close_ms,
            calendar="NYSE",
            validation=validation,
        )

    # Policy: DISABLED — skip read, write disabled receipt, return.
    if policy is HydratePolicy.DISABLED:
        _write_receipt(
            _base_receipt(
                accepted=False,
                validation=ValidationResult.failed("disabled_by_operator"),
            )
        )
        return

    # A strategy with no warm-startable state cannot satisfy — or fail — a
    # warm-start requirement: it never writes a sidecar (maybe_write skips a
    # None payload) and has nothing to restore. Treating the absent sidecar as
    # a REQUIRE "missing" failure here would exit 4 on EVERY session of such a
    # strategy (e.g. deployment_validation), so short-circuit to an accepted
    # cold-start receipt regardless of policy. The null global_sha256 /
    # sidecar_last_* fields signal that no sidecar was read.
    if not strategy.is_warm_startable():
        _write_receipt(_base_receipt(accepted=True, validation=ValidationResult.all_passed()))
        return

    # Check #1: schema parse + existence.
    try:
        envelope = repo.read()
    except Exception:
        receipt = _base_receipt(
            accepted=False,
            validation=ValidationResult.failed("schema_mismatch", schema_version_ok=False),
            global_sha=repo.sha256_of_on_disk(),
        )
        _write_receipt(receipt)
        if policy is HydratePolicy.REQUIRE:
            raise IndicatorStateHydrationError(receipt)
        return

    if envelope is None:
        receipt = _base_receipt(accepted=False, validation=ValidationResult.failed("missing"))
        _write_receipt(receipt)
        if policy is HydratePolicy.REQUIRE:
            raise IndicatorStateHydrationError(receipt)
        return

    global_sha = repo.sha256_of_on_disk()

    # Check #2: identity.
    if (
        envelope.strategy_key != strategy_key
        or envelope.symbol.upper() != symbol.upper()
        or envelope.consolidator_period_min != period
    ):
        receipt = _base_receipt(
            accepted=False,
            validation=ValidationResult.failed("identity_mismatch", identity_ok=False),
            sidecar_last_bar_ms=envelope.last_consolidated_bar_end_ms,
            global_sha=global_sha,
        )
        _write_receipt(receipt)
        if policy is HydratePolicy.REQUIRE:
            raise IndicatorStateHydrationError(receipt)
        return

    # Check #3: calendar — previous completed NYSE session.
    try:
        expected_prev_close_ms = previous_completed_session_close_ms(session_start_ms)
    except NoSessionError:
        receipt = _base_receipt(
            accepted=False,
            validation=ValidationResult.failed("calendar_stale", calendar_ok=False),
            sidecar_last_bar_ms=envelope.last_consolidated_bar_end_ms,
            global_sha=global_sha,
        )
        _write_receipt(receipt)
        if policy is HydratePolicy.REQUIRE:
            raise IndicatorStateHydrationError(receipt)
        return

    if envelope.last_consolidated_bar_end_ms != expected_prev_close_ms:
        receipt = _base_receipt(
            accepted=False,
            validation=ValidationResult.failed("calendar_stale", calendar_ok=False),
            sidecar_last_bar_ms=envelope.last_consolidated_bar_end_ms,
            expected_prev_close_ms=expected_prev_close_ms,
            global_sha=global_sha,
        )
        _write_receipt(receipt)
        if policy is HydratePolicy.REQUIRE:
            raise IndicatorStateHydrationError(receipt)
        return

    # Check #4: payload shape — strategy's own validator.
    shape_result: ValidationResult = strategy.validate_state_payload(envelope.payload)
    if shape_result.failure_reason is not None:
        receipt = _base_receipt(
            accepted=False,
            validation=shape_result,
            sidecar_last_bar_ms=envelope.last_consolidated_bar_end_ms,
            expected_prev_close_ms=expected_prev_close_ms,
            global_sha=global_sha,
        )
        _write_receipt(receipt)
        if policy is HydratePolicy.REQUIRE:
            raise IndicatorStateHydrationError(receipt)
        return

    # Check #5: indicators ready — samples >= period (RSI: period+1).
    if not _indicators_ready(envelope.payload):
        receipt = _base_receipt(
            accepted=False,
            validation=ValidationResult.failed("indicators_unready", indicators_ready_ok=False),
            sidecar_last_bar_ms=envelope.last_consolidated_bar_end_ms,
            expected_prev_close_ms=expected_prev_close_ms,
            global_sha=global_sha,
        )
        _write_receipt(receipt)
        if policy is HydratePolicy.REQUIRE:
            raise IndicatorStateHydrationError(receipt)
        return

    # Check #6: lifecycle flat.
    lifecycle = envelope.payload.get("lifecycle")
    if not isinstance(lifecycle, dict):
        receipt = _base_receipt(
            accepted=False,
            validation=ValidationResult.failed("lifecycle_not_flat", lifecycle_flat_ok=False),
            sidecar_last_bar_ms=envelope.last_consolidated_bar_end_ms,
            expected_prev_close_ms=expected_prev_close_ms,
            global_sha=global_sha,
        )
        _write_receipt(receipt)
        if policy is HydratePolicy.REQUIRE:
            raise IndicatorStateHydrationError(receipt)
        return
    if (
        lifecycle.get("position_qty", 0) != 0
        or lifecycle.get("pending_orders_count", 0) != 0
        or lifecycle.get("open_insights", 0) != 0
    ):
        receipt = _base_receipt(
            accepted=False,
            validation=ValidationResult.failed("lifecycle_not_flat", lifecycle_flat_ok=False),
            sidecar_last_bar_ms=envelope.last_consolidated_bar_end_ms,
            expected_prev_close_ms=expected_prev_close_ms,
            global_sha=global_sha,
        )
        _write_receipt(receipt)
        if policy is HydratePolicy.REQUIRE:
            raise IndicatorStateHydrationError(receipt)
        return

    # All checks passed — restore and write accepted receipt.
    try:
        strategy.restore_state_from_persistence(envelope.payload)
    except Exception as exc:
        # Nested-field validation failures inside restore_state escape
        # the per-strategy validate_state_payload shape check. Treat
        # any restore-time exception as a schema_mismatch so the
        # receipt is still written and the policy raise/return path is
        # honored (optional → cold-start, require → exit 4).
        receipt = _base_receipt(
            accepted=False,
            validation=ValidationResult.failed("schema_mismatch", schema_version_ok=False),
            sidecar_last_bar_ms=envelope.last_consolidated_bar_end_ms,
            expected_prev_close_ms=expected_prev_close_ms,
            global_sha=global_sha,
        )
        _write_receipt(receipt)
        if policy is HydratePolicy.REQUIRE:
            raise IndicatorStateHydrationError(receipt) from exc
        return
    _write_receipt(
        _base_receipt(
            accepted=True,
            validation=ValidationResult.all_passed(),
            sidecar_last_bar_ms=envelope.last_consolidated_bar_end_ms,
            expected_prev_close_ms=expected_prev_close_ms,
            global_sha=global_sha,
        )
    )


def _indicators_ready(payload: dict[str, Any]) -> bool:
    """Check each indicator block has samples >= period (RSI needs period+1).

    Per-strategy if-name-is-RSI behavior is intentional for PR1 — the
    SpyEma strategy is the only consumer, and RSI's is_ready predicate
    is genuinely different from SMA/EMA's. A generic refactor lives in
    a future base-class promotion.

    Defensive: a corrupted counter (e.g., samples="NaN") is treated as
    not-ready rather than crashing the ladder — this lets the policy
    (REQUIRE → exit 4, OPTIONAL → cold-start) handle the failure
    deterministically via the indicators_unready receipt path.
    """
    for key in ("ema5", "ema10", "rsi14"):
        block = payload.get(key)
        if not isinstance(block, dict):
            return False
        try:
            samples = int(block.get("samples", 0))
            period = int(block.get("period", 0))
        except (TypeError, ValueError):
            return False
        threshold = period + 1 if key.startswith("rsi") else period
        if samples < threshold:
            return False
    return True


def maybe_write(
    *,
    strategy: Any,
    artifacts_root: Path,
    reason: str,
    code_sha: str,
    strategy_spec_sha: str,
    last_consolidated_bar_end_ms: int,
) -> None:
    """Force-flat or graceful-shutdown checkpoint write.

    Skip without write if:
      * strategy.report_state_for_persistence returns None
      * reason == 'shutdown' and an on-disk envelope already has
        an equal-or-newer last_consolidated_bar_end_ms (the
        "newer check" — protects force-flat's canonical write).
    """
    payload = strategy.report_state_for_persistence()
    if payload is None:
        return

    if reason not in ("force_flat", "shutdown"):
        raise ValueError(f"unknown reason: {reason!r}")

    strategy_key: str = strategy.STRATEGY_KEY
    symbol: str = (
        strategy.ctx.symbols[0]
        if strategy.ctx is not None and strategy.ctx.symbols
        else getattr(strategy, "_symbol_name", "")
    )
    period: int = strategy.CONSOLIDATOR_PERIOD_MIN

    envelope = IndicatorStateEnvelope(
        schema_version=1,
        strategy_key=strategy_key,
        symbol=symbol,
        consolidator_period_min=period,
        last_consolidated_bar_end_ms=last_consolidated_bar_end_ms,
        captured_at_ms=int(_time.time() * 1000),
        captured_reason=reason,
        code_sha=code_sha,
        strategy_spec_sha=strategy_spec_sha,
        payload=payload,
    )
    repo = IndicatorStateRepo(stable_global_path(artifacts_root, strategy_key, symbol, period))
    if reason == "shutdown":
        repo.write_if_strictly_newer(envelope)
    else:
        repo.write(envelope)
