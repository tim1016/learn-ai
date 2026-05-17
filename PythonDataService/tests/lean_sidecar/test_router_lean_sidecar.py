"""Integration tests for /api/lean-sidecar/* endpoints.

Two layers:

* In-process (mocked launcher) — exercises the router → service →
  launcher_client edges using ``respx``. Runs everywhere.
* Real launcher (E2E) — gated on ``requires_lean_image`` so it only
  runs on hosts with the pinned LEAN image. That test lives in
  ``test_router_lean_sidecar_e2e.py`` to keep its conftest skip path
  independent of the mocked tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from app.lean_sidecar import config as sidecar_config
from app.lean_sidecar.launcher.models import LaunchResponse
from app.lean_sidecar.launcher_client import DEFAULT_LAUNCHER_URL
from app.lean_sidecar.workspace import resolve_workspace
from app.main import app

pytestmark = pytest.mark.asyncio


PINNED_DIGEST_FOR_TESTS = "sha256:00000000000000000000000000000000000000000000000000000000cafebabe"


@pytest.fixture
def patched_pin(monkeypatch: pytest.MonkeyPatch) -> str:
    """Pin a dummy image digest into config so the service does not
    refuse to launch for "no PINNED_LEAN_IMAGE_DIGEST" reasons."""
    monkeypatch.setattr(sidecar_config, "PINNED_LEAN_IMAGE_DIGEST", PINNED_DIGEST_FOR_TESTS)
    monkeypatch.setattr(
        sidecar_config,
        "ALLOWED_IMAGE_DIGESTS",
        frozenset({PINNED_DIGEST_FOR_TESTS}),
    )
    # Service reads PINNED_LEAN_IMAGE_DIGEST at module-import time
    # too; patch in-place.
    from app.services import lean_sidecar_service

    monkeypatch.setattr(lean_sidecar_service, "PINNED_LEAN_IMAGE_DIGEST", PINNED_DIGEST_FOR_TESTS)
    return PINNED_DIGEST_FOR_TESTS


@pytest.fixture
def patched_artifacts_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the service's artifacts root into a tmp dir per test
    so concurrent tests don't collide on workspace dirs."""
    root = (tmp_path / "artifacts").resolve()
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sidecar_config, "DEFAULT_ARTIFACTS_ROOT", root)
    from app.routers import lean_sidecar as lean_sidecar_router
    from app.services import lean_sidecar_service

    monkeypatch.setattr(lean_sidecar_service, "DEFAULT_ARTIFACTS_ROOT", root)
    monkeypatch.setattr(lean_sidecar_router, "DEFAULT_ARTIFACTS_ROOT", root)
    return root


@pytest.fixture
def stub_image_extract(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the image-bundled metadata extraction.

    Router-integration tests mock the launcher's HTTP surface and
    should not also need a real LEAN image present on the host just
    to exercise the staging seam. The stub writes the expected
    destination files so the manifest hashing step still has
    something to hash.
    """
    from app.services import lean_sidecar_service

    def _stub(workspace, image_digest):
        mh_dir = workspace.data_dir / "market-hours"
        sp_dir = workspace.data_dir / "symbol-properties"
        mh_dir.mkdir(parents=True, exist_ok=True)
        sp_dir.mkdir(parents=True, exist_ok=True)
        mh = mh_dir / "market-hours-database.json"
        sp = sp_dir / "symbol-properties-database.csv"
        mh.write_text("{}", encoding="utf-8")
        sp.write_text("symbol,market\n", encoding="utf-8")
        return mh, sp

    monkeypatch.setattr(lean_sidecar_service, "stage_lean_metadata_from_image", _stub)


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _good_payload(run_id: str = "router_unit") -> dict:
    return {
        "run_id": run_id,
        "symbol": "SPY",
        "start_date": "2025-01-06",
        "end_date": "2025-01-10",
        "starting_cash": 100000.0,
    }


def _launcher_success_body(run_id: str) -> dict:
    return LaunchResponse(
        run_id=run_id,
        exit_code=0,
        duration_ms=1234,
        timed_out=False,
        log_tail="ok",
        lean_errors={},
        is_clean=True,
    ).model_dump()


class TestPostTrustedRunValidation:
    @pytest.mark.parametrize(
        "bad_field,bad_value",
        [
            ("run_id", "../escape"),  # bad slug
            ("starting_cash", 0),  # below cap
            ("starting_cash", 50_000_000),  # above cap
        ],
    )
    async def test_pydantic_rejects_bad_inputs(
        self,
        client: AsyncClient,
        bad_field: str,
        bad_value: object,
    ) -> None:
        payload = _good_payload()
        payload[bad_field] = bad_value
        r = await client.post("/api/lean-sidecar/trusted-runs", json=payload)
        # Either 422 (Pydantic) or 400 (model_validator after) is OK;
        # both signal "request did not validate" before any container
        # work happens.
        assert r.status_code in (400, 422)

    async def test_reversed_window_rejected(self, client: AsyncClient) -> None:
        payload = _good_payload()
        payload["end_date"] = "2024-12-30"
        r = await client.post("/api/lean-sidecar/trusted-runs", json=payload)
        assert r.status_code == 422
        assert "end_date" in r.text

    async def test_oversized_window_rejected(self, client: AsyncClient) -> None:
        payload = _good_payload()
        payload["start_date"] = "2025-01-01"
        payload["end_date"] = "2025-03-01"
        r = await client.post("/api/lean-sidecar/trusted-runs", json=payload)
        assert r.status_code == 422
        assert "max" in r.text.lower() or "30" in r.text

    async def test_forbids_algorithm_source_field(self, client: AsyncClient) -> None:
        """Phase 2a refuses any algorithm_source field; that gate
        opens in Phase 3 per ADR §"Phase sequencing"."""
        payload = _good_payload()
        payload["algorithm_source"] = "class Evil(QCAlgorithm): pass"
        r = await client.post("/api/lean-sidecar/trusted-runs", json=payload)
        assert r.status_code == 422

    @pytest.mark.parametrize(
        "bad_symbol",
        [
            "../../etc/passwd",
            "SPY/extra",
            "SPY\\windows",
            "..",
            "",
            "TOO_LONG_TICKER_OVER_LIMIT_X",
        ],
    )
    async def test_pydantic_rejects_path_traversal_symbols(self, client: AsyncClient, bad_symbol: str) -> None:
        """Path-traversal characters in ``symbol`` must be rejected at
        the API boundary — before they reach the staging writers that
        join the symbol into a filesystem path."""
        payload = _good_payload()
        payload["symbol"] = bad_symbol
        r = await client.post("/api/lean-sidecar/trusted-runs", json=payload)
        assert r.status_code == 422, f"symbol {bad_symbol!r} should have been rejected at the boundary"


class TestPostTrustedRunHappyPath:
    async def test_launcher_clean_response_passes_through(
        self,
        client: AsyncClient,
        patched_pin: str,
        patched_artifacts_root: Path,
        stub_image_extract: None,
    ) -> None:
        payload = _good_payload("router_happy")
        async with respx.mock(base_url=DEFAULT_LAUNCHER_URL) as mock:
            mock.post("/launch").mock(return_value=httpx.Response(200, json=_launcher_success_body("router_happy")))
            r = await client.post("/api/lean-sidecar/trusted-runs", json=payload)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["run_id"] == "router_happy"
        assert body["is_clean"] is True
        assert body["lean_errors"]["analysis_failed"] == []
        # The orchestrator must have written the manifest before
        # returning — the manifest endpoint should resolve.
        ws = resolve_workspace("router_happy", patched_artifacts_root)
        assert ws.manifest_path.exists(), "manifest.json was not written"
        manifest = json.loads(ws.manifest_path.read_text(encoding="utf-8"))
        assert manifest["run_id"] == "router_happy"
        assert manifest["algorithm_type_name"] == "MyAlgorithm"
        assert manifest["lean_image_digest"] == patched_pin

    async def test_launcher_rejected_surfaces_as_400(
        self,
        client: AsyncClient,
        patched_pin: str,
        patched_artifacts_root: Path,
        stub_image_extract: None,
    ) -> None:
        async with respx.mock(base_url=DEFAULT_LAUNCHER_URL) as mock:
            mock.post("/launch").mock(
                return_value=httpx.Response(
                    400,
                    json={
                        "detail": {
                            "reason": "workspace_not_staged",
                            "message": "stage first",
                        }
                    },
                )
            )
            r = await client.post(
                "/api/lean-sidecar/trusted-runs",
                json=_good_payload("router_reject"),
            )
        assert r.status_code == 400
        assert r.json()["detail"]["reason"] == "workspace_not_staged"

    async def test_launcher_unreachable_surfaces_as_503(
        self,
        client: AsyncClient,
        patched_pin: str,
        patched_artifacts_root: Path,
        stub_image_extract: None,
    ) -> None:
        async with respx.mock(base_url=DEFAULT_LAUNCHER_URL) as mock:
            mock.post("/launch").mock(side_effect=httpx.ConnectError("refused"))
            r = await client.post(
                "/api/lean-sidecar/trusted-runs",
                json=_good_payload("router_unreach"),
            )
        assert r.status_code == 503
        assert r.json()["detail"]["reason"] == "launcher_unreachable"


class TestInspectionEndpoints:
    async def test_manifest_endpoint_returns_written_manifest(
        self,
        client: AsyncClient,
        patched_pin: str,
        patched_artifacts_root: Path,
        stub_image_extract: None,
    ) -> None:
        async with respx.mock(base_url=DEFAULT_LAUNCHER_URL) as mock:
            mock.post("/launch").mock(return_value=httpx.Response(200, json=_launcher_success_body("router_inspect")))
            await client.post(
                "/api/lean-sidecar/trusted-runs",
                json=_good_payload("router_inspect"),
            )
        r = await client.get("/api/lean-sidecar/runs/router_inspect/manifest")
        assert r.status_code == 200
        assert r.json()["run_id"] == "router_inspect"

    async def test_manifest_endpoint_404_for_unknown_run(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        r = await client.get("/api/lean-sidecar/runs/never_ran/manifest")
        assert r.status_code == 404

    async def test_observations_endpoint_404_when_missing(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        # Workspace exists (we resolve it) but no observations.csv.
        ws = resolve_workspace("ws_no_obs", patched_artifacts_root)
        ws.ensure_layout()
        r = await client.get("/api/lean-sidecar/runs/ws_no_obs/observations")
        assert r.status_code == 404
        assert r.json()["detail"]["reason"] == "observations_missing"

    async def test_log_endpoint_serves_tail(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        ws = resolve_workspace("ws_log", patched_artifacts_root)
        ws.ensure_layout()
        ws.lean_log_path.write_text("hello lean\n", encoding="utf-8")
        r = await client.get("/api/lean-sidecar/runs/ws_log/log")
        assert r.status_code == 200
        assert "hello lean" in r.text

    async def test_invalid_run_id_rejected_at_inspect(self, client: AsyncClient) -> None:
        r = await client.get("/api/lean-sidecar/runs/..escape/manifest")
        assert r.status_code == 400
