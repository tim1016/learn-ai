"""Transport for persisting a Recency Chart run snapshot to the .NET backend.

The recency-owned trade snapshot is the Recency Chart's sole source of
truth (design spec §4.1) — unlike the legacy engine/LEAN persistence
helpers (``persist_engine_run``, ``persist_via_dotnet``), which swallow
HTTP failures and return ``None`` because an on-disk artifact remains
authoritative, this function RAISES on any HTTP or transport failure. The
caller (``run_recency``) treats that exception as an isolated per-run
failure, so a persistence outage shows up as "N of M failed" rather than
a run silently vanishing with nothing durable written (code-review P0
finding on the original PRD).

Called from a plain worker thread (``ThreadPoolExecutor``, not an asyncio
task), so this uses a synchronous ``httpx.Client`` rather than the
``httpx.AsyncClient`` the legacy helpers use.
"""

from __future__ import annotations

from dataclasses import asdict

import httpx

from app.research.recency.runner import RecencyRunSnapshot


def persist_recency_snapshot(
    snapshot: RecencyRunSnapshot,
    *,
    base_url: str,
    timeout_seconds: float = 30.0,
) -> int:
    """POST a run snapshot to the backend; return the assigned RecencyRun id."""
    url = f"{base_url.rstrip('/')}/api/recency/snapshots"
    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.post(url, json=asdict(snapshot))
        response.raise_for_status()
        return int(response.json()["recency_run_id"])
