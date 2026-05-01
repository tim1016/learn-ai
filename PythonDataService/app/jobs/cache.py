"""Result cache for idempotent job dispatches.

When a user re-runs a feature/signal/cross-sectional job with the same
parameters, we serve the prior result from Redis instead of recomputing.
Cache hits skip the worker thread entirely — the dispatch route writes
the cached result back into the new ``job_id`` slot and emits
``job.completed`` immediately with ``cached=True`` so the UI can render
the report without a live-progress panel.

The cache key is the SHA-1 of the params, canonicalized to JSON with
sorted keys. Tickers are upper-cased so ``["spy", "QQQ"]`` and
``["SPY", "qqq"]`` collide (same study).

Failed and cancelled runs are NOT cached — only ``job.completed``
results land here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from app.jobs.progress import JOB_TTL_SECONDS, get_redis

logger = logging.getLogger(__name__)


def _result_cache_key(job_type: str, params_hash: str) -> str:
    return f"result:{job_type}:{params_hash}"


def params_hash(job_type: str, params: Mapping[str, Any]) -> str:
    """Canonical SHA-1 of (job_type, params).

    The job_type is folded into the hash so two job types can use the
    same params shape without colliding. Ticker lists are uppercased and
    sorted; date strings are passed through. Anything else is JSON-
    serialized with ``sort_keys=True``.
    """
    canonical = _canonicalize(params)
    blob = json.dumps({"type": job_type, "params": canonical}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _canonicalize(v) for k, v in sorted(value.items())}
    if isinstance(value, list | tuple):
        canon = [_canonicalize(v) for v in value]
        # Upper-case + sort tickers when the list looks like one (all strings).
        if canon and all(isinstance(v, str) for v in canon):
            return sorted(s.upper() for s in canon)
        return canon
    if isinstance(value, str):
        return value
    return value


def lookup(job_type: str, params: Mapping[str, Any]) -> tuple[str, dict] | None:
    """Return ``(hash, cached_result_dict)`` if a hit, else ``None``.

    The hash is returned alongside the result so the caller can ``store``
    a result back under the same key without re-canonicalizing.
    """
    h = params_hash(job_type, params)
    try:
        raw = get_redis().get(_result_cache_key(job_type, h))
    except Exception:
        # Cache is best-effort; never let it block real work.
        logger.warning("cache lookup failed for %s/%s", job_type, h, exc_info=True)
        return None
    if raw is None:
        return None
    try:
        return h, json.loads(raw)
    except json.JSONDecodeError:
        # Malformed cache entry — drop it and miss.
        return None


def store(job_type: str, params: Mapping[str, Any], result: Any) -> str:
    """Persist a result under (job_type, params_hash). Returns the hash."""
    h = params_hash(job_type, params)
    payload = {
        "result": result,
        "cached_at": int(time.time() * 1000),
    }
    try:
        get_redis().set(
            _result_cache_key(job_type, h),
            json.dumps(payload, default=str),
            ex=JOB_TTL_SECONDS,
        )
    except Exception:
        logger.warning("cache store failed for %s/%s", job_type, h, exc_info=True)
    return h


def serve_cached_result(job_id: str, job_type: str, cached: dict) -> None:
    """Write a cached result into a fresh job_id slot and emit terminal events.

    Mirrors what ``ProgressEmitter.completed`` does, plus a ``cached=True``
    marker on the completion event so the frontend can render the report
    without showing the live-progress panel.
    """
    from app.jobs.progress import _active_set_key, _result_key, _state_key  # local to avoid cycles

    r = get_redis()
    result = cached.get("result")
    cached_at = cached.get("cached_at", int(time.time() * 1000))
    now_ms = str(int(time.time() * 1000))

    pipe = r.pipeline()
    pipe.set(_result_key(job_id), json.dumps(result, default=str), ex=JOB_TTL_SECONDS)
    pipe.hset(
        _state_key(job_id),
        mapping={
            "status": "completed",
            "started_at": now_ms,
            "completed_at": now_ms,
            "result_kind": "json",
            "phase": "completed",
            "cached": "1",
        },
    )
    pipe.expire(_state_key(job_id), JOB_TTL_SECONDS)
    pipe.execute()

    # Emit the events on the new job_id stream so any subscriber sees a
    # complete lifecycle. Two events: started → completed (with cached=true).
    from app.jobs.progress import ProgressEmitter

    emitter = ProgressEmitter(job_id)
    emitter._emit("job.started", {"cached": True})
    emitter._emit(
        "job.completed",
        {
            "result_url": f"/api/jobs/{job_id}/result",
            "cached": True,
            "cached_at": cached_at,
        },
    )
    r.srem(_active_set_key(), job_id)
