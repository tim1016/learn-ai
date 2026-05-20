"""POST /api/lean-sidecar/lint contract tests."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_lint_endpoint_empty_source_returns_empty_diagnostics() -> None:
    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/lean-sidecar/lint", json={"source": ""})
        assert resp.status_code == 200
        assert resp.json() == {"diagnostics": []}


@pytest.mark.asyncio
async def test_lint_endpoint_unused_import_returns_f401() -> None:
    from app.main import app

    src = "import pandas\nclass X: pass\n"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/lean-sidecar/lint", json={"source": src})
        assert resp.status_code == 200
        rules = [d["rule"] for d in resp.json()["diagnostics"]]
        assert "F401" in rules


@pytest.mark.asyncio
async def test_lint_endpoint_oversize_returns_413() -> None:
    from app.lean_sidecar.config import MAX_ALGORITHM_SOURCE_BYTES
    from app.main import app

    huge = "x = 1\n" * (MAX_ALGORITHM_SOURCE_BYTES // 6 + 100)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/lean-sidecar/lint", json={"source": huge})
        assert resp.status_code == 413


@pytest.mark.asyncio
async def test_lint_endpoint_subprocess_timeout_returns_504(monkeypatch) -> None:
    """Monkey-patch the subprocess helper to never return; assert 504."""
    import asyncio

    from app.main import app
    from app.routers import lean_lint

    async def _hang(*args, **kwargs):
        await asyncio.sleep(100)

    monkeypatch.setattr(lean_lint, "_run_ruff", _hang)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/lean-sidecar/lint", json={"source": "x = 1"})
        assert resp.status_code == 504
