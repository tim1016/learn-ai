"""Tests for the memory-bounded ``/runs/{run_id}/log`` tail read.

The endpoint must NOT load the whole log into memory; it must seek to
the last 1 MiB and return only that. Tested with a synthetic log
large enough that a ``read_text()`` implementation would have been
visibly wasteful (the assertion is on the response body length, not
on RSS, but the endpoint code is also visibly seek-based).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.lean_sidecar import config as sidecar_config
from app.lean_sidecar.workspace import resolve_workspace
from app.main import app

pytestmark = pytest.mark.asyncio


_TAIL_CAP_BYTES = 1 << 20  # mirror router constant


@pytest.fixture
def patched_artifacts_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = (tmp_path / "artifacts").resolve()
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sidecar_config, "DEFAULT_ARTIFACTS_ROOT", root)
    from app.routers import lean_sidecar as lean_sidecar_router

    monkeypatch.setattr(lean_sidecar_router, "DEFAULT_ARTIFACTS_ROOT", root)
    return root


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestLogTail:
    async def test_small_log_returned_whole(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        ws = resolve_workspace("ws_small_log", patched_artifacts_root)
        ws.ensure_layout()
        # write_bytes to avoid the Windows ``\n``→``\r\n`` translation
        # that ``write_text`` does in text mode.
        ws.lean_log_path.write_bytes(b"hello\nworld\n")
        r = await client.get("/api/lean-sidecar/runs/ws_small_log/log")
        assert r.status_code == 200
        assert r.text == "hello\nworld\n"

    async def test_oversized_log_returns_only_tail(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        ws = resolve_workspace("ws_big_log", patched_artifacts_root)
        ws.ensure_layout()
        # Build a 3 MiB log with a recognisable head + recognisable
        # tail. The endpoint must drop the head bytes entirely.
        head_marker = b"HEAD-SHOULD-BE-DROPPED\n"
        tail_marker = b"\nTAIL-SHOULD-BE-PRESENT"
        filler = b"x" * (3 * _TAIL_CAP_BYTES)
        ws.lean_log_path.write_bytes(head_marker + filler + tail_marker)

        r = await client.get("/api/lean-sidecar/runs/ws_big_log/log")
        assert r.status_code == 200
        # Body is bounded by the cap, never the full file.
        assert len(r.content) <= _TAIL_CAP_BYTES
        # Head marker fell off the tail; tail marker survived.
        assert b"HEAD-SHOULD-BE-DROPPED" not in r.content
        assert b"TAIL-SHOULD-BE-PRESENT" in r.content
