"""Append-only JSONL capture journal (Broker System v2, §6).

The journal is the **broker-neutral** raw-capture medium: every vendor
response — success *or* error — is recorded verbatim before any SDK parsing,
so the on-disk record is exactly what the wire delivered. Files are canonical
(design decision D3); any future Postgres projection is rebuildable from these
files and never authoritative.

Layout::

    <BROKER_CAPTURE_DIR>/<broker>/<endpoint-family>/<YYYY-MM-DD>.jsonl

rotated by UTC day. One line per response::

    {"broker","endpoint","method","params","status","captured_at_ms","raw_body"}

with an optional ``"body_encoding":"base64"`` when the body is not valid UTF-8.

Invariants:

- **Verbatim.** ``raw_body`` round-trips to the exact response bytes.
- **All responses, including errors.** A 403 is evidence too.
- **No secrets.** ``params`` carries query/body parameters only; secret-like
  keys are redacted here as defence in depth (the vendor capture hook is the
  first line — it never forwards auth headers or key material).
- **Non-fatal on the read path (phase 1).** A capture failure logs ERROR and
  increments an observable counter; it never breaks the caller's request. The
  phase-2 order path flips this to fail-closed (no journal → no order).
- **Single writer per process.** Appends are serialized by a lock and flushed
  per line; ``fsync`` is deferred to the phase-2 order path.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# PythonDataService/ — app/broker/capture/journal.py → parents[3].
_SERVICE_ROOT = Path(__file__).resolve().parents[3]

# Placeholder written in place of any redacted value.
REDACTED = "***REDACTED***"

# A path component (broker id, endpoint family) may only contain these
# characters. This is the CodeQL-recognised sanitiser: an unsafe component
# never reaches the filesystem path, so traversal (``..``, separators) is
# impossible by construction.
_SAFE_COMPONENT = re.compile(r"^[a-z0-9_-]+$")

# Query/body parameter keys whose values must never be journaled. Matched
# case-insensitively as substrings so ``ALPACA_API_KEY_ID`` and ``apca-secret``
# are both caught.
_SECRET_KEY = re.compile(r"key|secret|token|password|authorization|apca", re.IGNORECASE)


class CaptureEndpoint(StrEnum):
    """The capture endpoint families (each rotates its own daily file).

    The six REST families landed in phase 1; ``STREAM`` (phase 2, S4) is the
    verbatim-capture family for the ``trade_updates`` websocket. Every raw frame
    — the auth handshake, subscribe ack, and each lifecycle event — is journaled
    under this family before it is parsed, with secret-like keys redacted (the
    auth frame's ``key_id``/``secret_key`` are caught by the shared redaction).
    """

    ACCOUNT = "account"
    POSITIONS = "positions"
    ORDERS = "orders"
    ACTIVITIES = "activities"
    ASSETS = "assets"
    CLOCK = "clock"
    STREAM = "stream"


class CaptureSettings(BaseSettings):
    """Capture-layer settings.

    ``BROKER_CAPTURE_DIR`` is intentionally **not** ``ALPACA_``-prefixed — the
    capture layer is broker-neutral. Default lives under the service's
    git-ignored ``var/`` tree.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="BROKER_CAPTURE_",
        case_sensitive=False,
        extra="ignore",
    )

    dir: Path = _SERVICE_ROOT / "var" / "broker_captures"


def _default_clock() -> int:
    """Return the current instant as ``int64`` ms since Unix epoch UTC."""
    return int(datetime.now(UTC).timestamp() * 1000)


def _utc_day(captured_at_ms: int) -> str:
    """Return the UTC calendar day (``YYYY-MM-DD``) for a ms timestamp."""
    return datetime.fromtimestamp(captured_at_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def _redact_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively replace values of secret-like keys with a placeholder."""
    return {
        key: (
            REDACTED
            if _SECRET_KEY.search(str(key))
            else _redact_value(value)
        )
        for key, value in params.items()
    }


def _redact_value(value: Any) -> Any:
    """Redact nested mappings and sequences without changing scalar values."""
    if isinstance(value, Mapping):
        return _redact_params(value)
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


def _encode_body(raw_body: bytes) -> tuple[str, str | None]:
    """Return ``(text, body_encoding)``; base64 when the body is not UTF-8."""
    try:
        return raw_body.decode("utf-8"), None
    except UnicodeDecodeError:
        return base64.b64encode(raw_body).decode("ascii"), "base64"


class CaptureJournal:
    """Single-writer, append-only JSONL journal of raw broker responses."""

    def __init__(
        self,
        *,
        capture_dir: Path | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._root = Path(capture_dir) if capture_dir is not None else _get_settings().dir
        self._clock = clock or _default_clock
        self._lock = threading.Lock()
        self._records_written = 0
        self._failure_count = 0

    @property
    def failure_count(self) -> int:
        """Number of capture attempts that failed (observable counter)."""
        with self._lock:
            return self._failure_count

    @property
    def records_written(self) -> int:
        """Number of records successfully appended (observable counter)."""
        with self._lock:
            return self._records_written

    def record(
        self,
        *,
        broker: str,
        endpoint: str,
        method: str,
        params: Mapping[str, Any] | None,
        status: int,
        raw_body: bytes,
    ) -> bool:
        """Append one capture line. Never raises — returns ``True`` on success.

        A failure (bad path component, unwritable directory, serialization
        error) logs ERROR and increments :pyattr:`failure_count` but leaves the
        caller's request unaffected, per the phase-1 read-path failure policy.
        """
        try:
            broker_component = _safe_component(broker, "broker")
            endpoint_component = _safe_component(endpoint, "endpoint")
            captured_at_ms = self._clock()
            text, body_encoding = _encode_body(raw_body)

            record: dict[str, Any] = {
                "broker": broker,
                "endpoint": endpoint,
                "method": method.upper(),
                "params": _redact_params(params or {}),
                "status": status,
                "captured_at_ms": captured_at_ms,
                "raw_body": text,
            }
            if body_encoding is not None:
                record["body_encoding"] = body_encoding

            line = json.dumps(record, ensure_ascii=False, default=str)
            path = self._path_for(broker_component, endpoint_component, captured_at_ms)
            with self._lock:
                self._append_line(path, line)
                self._records_written += 1
        except Exception:  # capture is best-effort; never fatal on the read path.
            with self._lock:
                self._failure_count += 1
            logger.error(
                "broker capture failed",
                extra={"broker": broker, "endpoint": endpoint, "status": status},
                exc_info=True,
            )
            return False

        return True

    def _path_for(self, broker: str, endpoint: str, captured_at_ms: int) -> Path:
        """Resolve the day file, guaranteeing it stays under the journal root."""
        root = self._root.resolve()
        path = (root / broker / endpoint / f"{_utc_day(captured_at_ms)}.jsonl").resolve()
        # Defence in depth: components are already charset-validated, so this
        # can only fail on a symlinked root — treat that as a fatal capture bug.
        if not str(path).startswith(str(root)):
            raise ValueError(f"capture path escapes journal root: {path}")
        return path

    def _append_line(self, path: Path, line: str) -> None:
        """Append and flush one line while the caller holds ``_lock``."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()


def _safe_component(value: str, kind: str) -> str:
    """Return a lowercased, traversal-safe path component or raise."""
    lowered = value.lower()
    if not _SAFE_COMPONENT.match(lowered):
        raise ValueError(f"unsafe {kind} path component: {value!r}")
    return lowered


_settings: CaptureSettings | None = None
_journal: CaptureJournal | None = None


def _get_settings() -> CaptureSettings:
    global _settings
    if _settings is None:
        _settings = CaptureSettings()
    return _settings


def get_capture_journal() -> CaptureJournal:
    """Return the process-wide capture journal (single writer per process)."""
    global _journal
    if _journal is None:
        _journal = CaptureJournal()
    return _journal


def reset_capture_journal_for_testing() -> None:
    """Drop the cached settings and journal so tests can rebind the env."""
    global _settings, _journal
    _settings = None
    _journal = None
