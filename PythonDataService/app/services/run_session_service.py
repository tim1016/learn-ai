"""In-memory run-session registry for the streaming dataset pipeline.

A "run" is a single Fetch & bundle invocation. The frontend opens an SSE
stream which receives progress events; the worker thread runs the existing
sync pipeline (chunked Polygon fetch + indicator preprocessing + ZIP build)
and emits events through a callback. The final ZIP is held here keyed by
session id until the frontend retrieves it via GET, or the user cancels.

Why in-memory: a single FastAPI worker, short-lived sessions (tens of
seconds at most), and the ZIP is meant to be downloaded immediately. If we
ever scale to multiple workers, swap this for a shared store (Redis) without
changing the public API.

Cleanup: each session carries a ``created_at``; a janitor reaper sweeps
sessions older than ``MAX_AGE_S`` on every new ``create``. Cancel sets a
threading.Event the worker polls between chunks.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Sessions older than this are reaped. 10 minutes covers any realistic
# Starter-plan fetch (maxes out around 5 minutes for 2 years of 1-min bars
# split across 24 chunks) with margin.
_MAX_AGE_S = 600.0


@dataclass
class RunSession:
    """One Fetch & bundle invocation. The worker fills ``zip_bytes`` and
    ``filename`` on completion; the frontend retrieves them via GET and we
    immediately drop the entry from the registry."""

    id: str
    created_at: float
    cancelled: threading.Event = field(default_factory=threading.Event)
    zip_bytes: bytes | None = None
    filename: str | None = None
    failed: bool = False
    error_message: str | None = None


class RunSessionRegistry:
    """Thread-safe map of session id → RunSession."""

    def __init__(self) -> None:
        self._sessions: dict[str, RunSession] = {}
        self._lock = threading.Lock()

    def create(self) -> RunSession:
        self._reap_expired_locked_unsafe()
        sid = uuid.uuid4().hex
        session = RunSession(id=sid, created_at=time.monotonic())
        with self._lock:
            self._sessions[sid] = session
        logger.info("[RUN] Created session %s", sid)
        return session

    def get(self, session_id: str) -> RunSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def pop(self, session_id: str) -> RunSession | None:
        """Retrieve and remove. Used by the binary retrieval endpoint so a
        ZIP is only downloadable once."""
        with self._lock:
            return self._sessions.pop(session_id, None)

    def cancel(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return False
        session.cancelled.set()
        logger.info("[RUN] Cancelled session %s", session_id)
        return True

    def _reap_expired_locked_unsafe(self) -> None:
        """Drop sessions older than MAX_AGE_S. Called under no lock; we take
        the lock briefly to swap the dict so we don't hold it across the
        sweep."""
        now = time.monotonic()
        with self._lock:
            stale = [sid for sid, s in self._sessions.items() if now - s.created_at > _MAX_AGE_S]
            for sid in stale:
                self._sessions.pop(sid, None)
        if stale:
            logger.info("[RUN] Reaped %d stale session(s)", len(stale))


# Module-level singleton — same lifetime as the FastAPI process.
run_sessions = RunSessionRegistry()
