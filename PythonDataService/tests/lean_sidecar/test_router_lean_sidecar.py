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
def stub_normalized_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the normalized parser for mocked launcher tests.

    The mocked launcher never produces real LEAN output, so calling
    the real parser would always raise ``NormalizedParserError``. The
    service handles that gracefully (the run still completes with
    ``normalized=None``); this fixture makes the failure deterministic
    instead of relying on incidental ENOENT.
    """
    from app.lean_sidecar.normalized_parser import NormalizedParserError
    from app.services import lean_sidecar_service

    def _stub(workspace):
        raise NormalizedParserError("stubbed in mocked-launcher tests")

    monkeypatch.setattr(lean_sidecar_service, "parse_workspace", _stub)


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# 2025-01-06 00:00 UTC = 1_736_121_600_000 ms; 2025-01-10 00:00 UTC =
# 1_736_467_200_000 ms. The trusted-sample window is [Mon, Fri] = 5
# trading days; under the 30-day cap.
_GOOD_START_MS = 1_736_121_600_000
_GOOD_END_MS = 1_736_467_200_000


def _good_payload(run_id: str = "router_unit") -> dict:
    return {
        "run_id": run_id,
        "symbol": "SPY",
        "start_ms_utc": _GOOD_START_MS,
        "end_ms_utc": _GOOD_END_MS,
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
        # 422 specifically: every rejection in this parametrize is a
        # Pydantic field/model_validator violation, not a downstream
        # service error. Locking to 422 catches regressions where a
        # request-shape error accidentally becomes a 400.
        assert r.status_code == 422

    async def test_reversed_window_rejected(self, client: AsyncClient) -> None:
        payload = _good_payload()
        payload["end_ms_utc"] = _GOOD_START_MS - 86_400_000  # 1 day before start
        r = await client.post("/api/lean-sidecar/trusted-runs", json=payload)
        assert r.status_code == 422
        assert "end_ms_utc" in r.text or "strictly greater" in r.text

    async def test_oversized_window_rejected(self, client: AsyncClient) -> None:
        payload = _good_payload()
        # 60 calendar days = ~42 weekdays, over the 30-trading-day cap.
        payload["start_ms_utc"] = _GOOD_START_MS
        payload["end_ms_utc"] = _GOOD_START_MS + 60 * 86_400_000
        r = await client.post("/api/lean-sidecar/trusted-runs", json=payload)
        assert r.status_code == 422
        assert "trading days" in r.text or "30" in r.text

    async def test_weekends_only_window_rejected(self, client: AsyncClient) -> None:
        """A window covering only weekends must be rejected — zero
        weekdays means staging would produce zero bars, which is the
        kind of silent-empty failure the router has to catch up
        front."""
        # 2025-01-04 (Sat) 00:00 UTC = 1_735_948_800_000
        # 2025-01-05 (Sun) 23:59 UTC = 1_736_121_540_000
        payload = _good_payload()
        payload["start_ms_utc"] = 1_735_948_800_000
        payload["end_ms_utc"] = 1_736_121_540_000
        r = await client.post("/api/lean-sidecar/trusted-runs", json=payload)
        assert r.status_code == 422
        assert "weekday" in r.text.lower() or "trading day" in r.text.lower()

    async def test_forbids_unknown_extra_fields(self, client: AsyncClient) -> None:
        """``extra="forbid"`` still rejects keys the schema doesn't
        know about. ``algorithm_source`` IS in the schema as of
        Phase 4c (see separate tests), so this test uses a different
        bogus field that proves the forbid-unknown contract still
        holds — important because a future field could be smuggled
        if forbid silently became allow."""
        payload = _good_payload()
        payload["unknown_field"] = "anything"
        r = await client.post("/api/lean-sidecar/trusted-runs", json=payload)
        assert r.status_code == 422

    async def test_algorithm_source_optional(self) -> None:
        """Phase 4c: omitting ``algorithm_source`` is valid — the
        server falls back to the trusted sample. This is a schema-only
        test (no HTTP round-trip) so it does not depend on the host
        having podman or the LEAN image — Phase 1c sandbox wiring is
        tested separately in ``test_router_lean_sidecar_e2e.py``."""
        from app.routers.lean_sidecar import TrustedRunRequestModel

        payload = _good_payload()
        assert "algorithm_source" not in payload
        model = TrustedRunRequestModel.model_validate(payload)
        assert model.algorithm_source is None

    async def test_algorithm_source_empty_string_rejected(self, client: AsyncClient) -> None:
        payload = _good_payload()
        payload["algorithm_source"] = "   \n\t  "
        r = await client.post("/api/lean-sidecar/trusted-runs", json=payload)
        assert r.status_code == 422
        assert "empty" in r.text.lower() or "whitespace" in r.text.lower()

    async def test_algorithm_source_oversize_rejected(self, client: AsyncClient) -> None:
        """Phase 4c: ADR-mandated 256 KiB cap on user source. Exceeding
        must 422 before the launcher round-trip."""
        payload = _good_payload()
        # 300 KiB of ASCII — over the 256 KiB cap.
        payload["algorithm_source"] = "x" * (300 * 1024)
        r = await client.post("/api/lean-sidecar/trusted-runs", json=payload)
        assert r.status_code == 422
        assert "bytes" in r.text.lower() or "max" in r.text.lower()

    async def test_algorithm_source_within_cap_accepted(self) -> None:
        """Right at the boundary — a 200 KiB source must pass schema.
        Schema-only assertion: a real HTTP round-trip on CI would need
        podman + the LEAN image, which are Phase 1c E2E concerns and
        live in ``test_router_lean_sidecar_e2e.py``."""
        from app.routers.lean_sidecar import TrustedRunRequestModel

        payload = _good_payload()
        payload["algorithm_source"] = "# " + "x" * (200 * 1024)
        model = TrustedRunRequestModel.model_validate(payload)
        assert model.algorithm_source is not None
        assert len(model.algorithm_source.encode("utf-8")) == 200 * 1024 + 2

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
        stub_normalized_parser: None,
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
        stub_normalized_parser: None,
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
        stub_normalized_parser: None,
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
        stub_normalized_parser: None,
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

    async def test_normalized_endpoint_returns_written_result(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        """Phase 3a: /normalized serves the parsed result.json
        written by the orchestrator."""
        ws = resolve_workspace("ws_normalized", patched_artifacts_root)
        ws.ensure_layout()
        ws.normalized_dir.mkdir(parents=True, exist_ok=True)
        payload = {"parser_version": "phase-3a-r1", "algorithm_id": "MyAlgorithm"}
        (ws.normalized_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")
        r = await client.get("/api/lean-sidecar/runs/ws_normalized/normalized")
        assert r.status_code == 200
        assert r.json()["parser_version"] == "phase-3a-r1"

    async def test_normalized_endpoint_404_when_missing(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        ws = resolve_workspace("ws_no_normalized", patched_artifacts_root)
        ws.ensure_layout()
        r = await client.get("/api/lean-sidecar/runs/ws_no_normalized/normalized")
        assert r.status_code == 404
        assert r.json()["detail"]["reason"] == "normalized_missing"

    async def test_invalid_run_id_rejected_at_inspect(self, client: AsyncClient) -> None:
        r = await client.get("/api/lean-sidecar/runs/..escape/manifest")
        assert r.status_code == 400


def _write_manifest(
    artifacts_root: Path,
    run_id: str,
    *,
    symbol: str = "SPY",
    started_at_ms: int | None = 1_736_121_600_000,
    finished_at_ms: int | None = 1_736_121_605_000,
    exit_code: int | None = 0,
    algorithm_source_kind: str | None = "trusted_sample",
    is_clean: bool | None = None,
) -> None:
    """Write a minimal manifest.json into a run's workspace dir.

    Just enough fields for the index endpoint's summary extractor; not
    a full manifest. Real manifests come from ``write_manifest`` in
    other tests."""
    run_dir = artifacts_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    notes = []
    if algorithm_source_kind is not None:
        notes.append(f"algorithm_source_kind={algorithm_source_kind}")
    if is_clean is not None:
        notes.append(f"is_clean={is_clean}")
    body = {
        "run_id": run_id,
        "parameters": {"symbol": symbol, "starting_cash": "100000.0"},
        "requested_window_ms": {"start_ms": 1_736_121_600_000, "end_ms": 1_736_467_200_000},
        "started_at_ms": started_at_ms,
        "finished_at_ms": finished_at_ms,
        "exit_code": exit_code,
        "notes": notes,
    }
    (run_dir / "manifest.json").write_text(json.dumps(body), encoding="utf-8")


class TestRunsIndex:
    async def test_empty_artifacts_root_returns_empty_list(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        r = await client.get("/api/lean-sidecar/runs")
        assert r.status_code == 200
        body = r.json()
        assert body["runs"] == []
        assert body["truncated"] is False
        assert body["cap"] >= 1

    async def test_lists_runs_in_started_at_desc(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        _write_manifest(patched_artifacts_root, "ui_run_001", started_at_ms=1000)
        _write_manifest(patched_artifacts_root, "ui_run_002", started_at_ms=3000)
        _write_manifest(patched_artifacts_root, "ui_run_003", started_at_ms=2000)
        r = await client.get("/api/lean-sidecar/runs")
        assert r.status_code == 200
        ids = [row["run_id"] for row in r.json()["runs"]]
        assert ids == ["ui_run_002", "ui_run_003", "ui_run_001"]

    async def test_skips_directories_without_manifest(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        # An in-progress workspace exists but has no manifest yet — it
        # must not appear in the index (otherwise we'd render a "run"
        # with no symbol/window/exit data).
        (patched_artifacts_root / "ui_run_pending").mkdir()
        _write_manifest(patched_artifacts_root, "ui_run_finished", started_at_ms=1000)
        r = await client.get("/api/lean-sidecar/runs")
        ids = [row["run_id"] for row in r.json()["runs"]]
        assert ids == ["ui_run_finished"]

    async def test_skips_unparseable_manifest(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        # A half-written manifest from a launcher crash mid-write must
        # not 500 the whole index — silently skip the bad row.
        bad_dir = patched_artifacts_root / "ui_run_corrupt"
        bad_dir.mkdir()
        (bad_dir / "manifest.json").write_text("{not valid json", encoding="utf-8")
        _write_manifest(patched_artifacts_root, "ui_run_ok", started_at_ms=1000)
        r = await client.get("/api/lean-sidecar/runs")
        assert r.status_code == 200
        ids = [row["run_id"] for row in r.json()["runs"]]
        assert ids == ["ui_run_ok"]

    async def test_skips_non_slug_directory_names(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        # A stray dir from an out-of-band tool — e.g., a tar extract —
        # whose name fails the slug regex must not be enumerated. We
        # don't want to render arbitrary host paths in the sidebar.
        stray = patched_artifacts_root / "Not A Slug!"
        stray.mkdir()
        (stray / "manifest.json").write_text("{}", encoding="utf-8")
        _write_manifest(patched_artifacts_root, "ui_run_valid", started_at_ms=1000)
        r = await client.get("/api/lean-sidecar/runs")
        ids = [row["run_id"] for row in r.json()["runs"]]
        assert ids == ["ui_run_valid"]

    async def test_summary_fields_extracted_from_manifest(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        _write_manifest(
            patched_artifacts_root,
            "ui_run_full",
            symbol="AAPL",
            started_at_ms=1_700_000_000_000,
            finished_at_ms=1_700_000_001_000,
            exit_code=0,
            algorithm_source_kind="user_provided",
            is_clean=True,
        )
        r = await client.get("/api/lean-sidecar/runs")
        row = r.json()["runs"][0]
        assert row["run_id"] == "ui_run_full"
        assert row["symbol"] == "AAPL"
        assert row["started_at_ms"] == 1_700_000_000_000
        assert row["finished_at_ms"] == 1_700_000_001_000
        assert row["exit_code"] == 0
        assert row["exit_clean"] is True
        assert row["is_clean"] is True
        assert row["algorithm_source_kind"] == "user_provided"
        assert row["requested_start_ms_utc"] == 1_736_121_600_000
        assert row["requested_end_ms_utc"] == 1_736_467_200_000

    async def test_exit_clean_false_when_exit_code_nonzero(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        _write_manifest(patched_artifacts_root, "ui_run_failed", exit_code=137)
        row = (await client.get("/api/lean-sidecar/runs")).json()["runs"][0]
        assert row["exit_code"] == 137
        assert row["exit_clean"] is False

    async def test_legacy_manifest_without_source_kind_is_unknown(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        # Pre-Phase-4c manifests don't have the source-kind note. The
        # index must not misclassify those as 'trusted_sample' (which
        # would lie about a possibly-user-source run); 'unknown' is
        # the honest answer.
        _write_manifest(patched_artifacts_root, "ui_run_legacy", algorithm_source_kind=None)
        row = (await client.get("/api/lean-sidecar/runs")).json()["runs"][0]
        assert row["algorithm_source_kind"] == "unknown"

    async def test_is_clean_parsed_from_manifest_note_false(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        """Reviewer P1: a manifest noting ``is_clean=False`` (e.g., LEAN
        exited 0 but logged data/analysis errors) must surface as
        ``is_clean: false`` in the row so the sidebar click does not
        synthesize a green "Clean run" badge."""
        _write_manifest(
            patched_artifacts_root,
            "ui_run_dirty_zero_exit",
            exit_code=0,
            is_clean=False,
        )
        row = (await client.get("/api/lean-sidecar/runs")).json()["runs"][0]
        # exit_code is 0 → exit_clean is True (just a status code check),
        # but the manifest's is_clean note is False because LEAN logged
        # errors. The UI must branch on the latter.
        assert row["exit_code"] == 0
        assert row["exit_clean"] is True
        assert row["is_clean"] is False

    async def test_is_clean_parsed_from_manifest_note_true(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        _write_manifest(patched_artifacts_root, "ui_run_truly_clean", is_clean=True)
        row = (await client.get("/api/lean-sidecar/runs")).json()["runs"][0]
        assert row["is_clean"] is True

    async def test_is_clean_null_for_legacy_manifest_without_note(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        # Pre-Phase-2a manifests don't carry the is_clean note. ``None``
        # is the honest answer — under-claim, never guess.
        _write_manifest(patched_artifacts_root, "ui_run_legacy_no_clean_note", is_clean=None)
        row = (await client.get("/api/lean-sidecar/runs")).json()["runs"][0]
        assert row["is_clean"] is None

    async def test_cap_applied_after_global_sort(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reviewer P2: cap must be applied after the started_at_ms
        sort, not during the directory-scan loop. Pre-Phase-4d
        run_ids didn't have a millisecond suffix, so a legacy run
        with a lexically-late slug could push a genuinely-newer run
        past the cap if the truncation happened scan-side.

        Shrink the cap to 2 so the bug surfaces with a 3-run setup."""
        from app.routers import lean_sidecar as lean_sidecar_router

        monkeypatch.setattr(lean_sidecar_router, "_RUN_INDEX_CAP", 2)
        monkeypatch.setattr(lean_sidecar_router, "_SCAN_HARD_CAP", 10)
        # Three runs: lexical order is ui_run_z, ui_run_m, ui_run_a.
        # Started-at order (desc) is ui_run_a, ui_run_z, ui_run_m.
        # With cap=2 applied after sort we expect [a, z]; applied
        # during scan (the bug) we'd get [z, m].
        _write_manifest(patched_artifacts_root, "ui_run_a", started_at_ms=3000)
        _write_manifest(patched_artifacts_root, "ui_run_m", started_at_ms=1000)
        _write_manifest(patched_artifacts_root, "ui_run_z", started_at_ms=2000)
        body = (await client.get("/api/lean-sidecar/runs")).json()
        ids = [row["run_id"] for row in body["runs"]]
        assert ids == ["ui_run_a", "ui_run_z"]
        assert body["truncated"] is True

    async def test_truncated_false_when_under_cap(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        _write_manifest(patched_artifacts_root, "ui_run_alpha", started_at_ms=1000)
        body = (await client.get("/api/lean-sidecar/runs")).json()
        assert body["truncated"] is False
        assert len(body["runs"]) == 1

    async def test_skips_schema_invalid_manifest_with_log(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """CodeRabbit finding: a manifest that parses as JSON but has
        a typed field of the wrong type (here ``started_at_ms`` is a
        string) must be skipped — and the skip must be logged, not
        swallowed silently, per the no-silent-exceptions rule."""
        bad_dir = patched_artifacts_root / "ui_run_badtypes"
        bad_dir.mkdir()
        bad = {
            "run_id": "ui_run_badtypes",
            "parameters": {"symbol": "SPY"},
            "requested_window_ms": {"start_ms": 1, "end_ms": 2},
            "started_at_ms": "not a number",  # ← wrong type, fails RunSummaryModel
            "exit_code": 0,
        }
        (bad_dir / "manifest.json").write_text(json.dumps(bad), encoding="utf-8")
        _write_manifest(patched_artifacts_root, "ui_run_ok", started_at_ms=1000)

        import logging

        with caplog.at_level(logging.WARNING, logger="app.routers.lean_sidecar"):
            r = await client.get("/api/lean-sidecar/runs")

        assert r.status_code == 200
        ids = [row["run_id"] for row in r.json()["runs"]]
        assert ids == ["ui_run_ok"]
        # Skip must be logged with the bad run_id for diagnosis.
        assert any("ui_run_badtypes" in rec.message for rec in caplog.records)


class TestPostReconcileEndpoint:
    """Phase 5a — POST /runs/{id}/reconcile integration tests.

    Uses the same `_stub_parse_workspace` pattern as the inspection
    endpoints — we mock `parse_workspace` to return a stubbed
    `NormalizedResult` rather than write LEAN-shaped artifact files into
    the workspace, because the reconciler logic is what's under test
    here, not the parser."""

    @pytest.fixture
    def stub_normalized_result(self, monkeypatch: pytest.MonkeyPatch):
        """Make `parse_workspace` return a callable factory so each test
        can inject its own NormalizedResult."""
        from app.lean_sidecar.normalized_parser import NormalizedResult
        from app.routers import lean_sidecar as router_module

        def _make_factory(events: list[dict], *, algorithm_id: str = "MyAlgorithm"):
            from app.lean_sidecar.normalized_parser import NormalizedOrderEvent

            order_events = [NormalizedOrderEvent.model_validate(e) for e in events]
            result = NormalizedResult(
                parser_version="phase-3a-r1",
                algorithm_id=algorithm_id,
                statistics={},
                runtime_statistics={},
                equity_curve=[],
                order_events=order_events,
                total_order_events=len(order_events),
                total_equity_points=0,
            )

            def _parse(workspace) -> NormalizedResult:
                return result

            monkeypatch.setattr(router_module, "parse_workspace", _parse)
            return result

        return _make_factory

    def _filled_event(self, **overrides) -> dict:
        base = {
            "order_event_id": 1,
            "order_id": 100,
            "algorithm_id": "MyAlgorithm",
            "symbol": "SPY",
            "symbol_value": "SPY",
            "ms_utc": 1_736_121_600_000,
            "status": "Filled",
            "direction": "Buy",
            "quantity": 100.0,
            "fill_price": 580.50,
            "fill_price_currency": "USD",
            "fill_quantity": 100.0,
            "is_assignment": False,
            "order_fee_amount": 1.00,
            "order_fee_currency": "USD",
            "message": None,
        }
        base.update(overrides)
        return base

    async def test_clean_run_returns_empty_divergences(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
        stub_normalized_result,
    ) -> None:
        ws = resolve_workspace("rec_clean", patched_artifacts_root)
        ws.ensure_layout()
        stub_normalized_result([self._filled_event(order_fee_amount=1.00)])

        r = await client.post("/api/lean-sidecar/runs/rec_clean/reconcile")

        assert r.status_code == 200
        body = r.json()
        # Reviewer P1: run_id is the workspace slug (path param), not the
        # algorithm-type-name. They diverge in every real run because the
        # slug is a UI-generated UUID-ish token and the algorithm-id
        # defaults to ``MyAlgorithm``.
        assert body["run_id"] == "rec_clean"
        assert body["algorithm_id"] == "MyAlgorithm"
        assert body["run_id"] != body["algorithm_id"]
        assert body["total_fill_events"] == 1
        assert body["matched_count"] == 1
        assert body["divergent_count"] == 0
        assert body["divergences"] == []
        assert body["commission_atol"] == "0.01"
        assert body["total_recorded_fees"] == "1.00"
        assert body["total_expected_ibkr_fees"] == "1.00"

    async def test_run_id_in_report_is_path_param_not_algorithm_id(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
        stub_normalized_result,
    ) -> None:
        """Reviewer P1 regression: the report's ``run_id`` field must be
        the workspace slug from the URL, not LEAN's algorithm-type-name.
        Before the fix, POST /runs/my_actual_run/reconcile would return
        ``run_id: "MyAlgorithm"`` because the wrapper used result.algorithm_id."""
        ws = resolve_workspace("rec_path_runid", patched_artifacts_root)
        ws.ensure_layout()
        stub_normalized_result(
            [self._filled_event(order_fee_amount=1.00)],
            algorithm_id="SomeOtherAlgo",
        )

        r = await client.post("/api/lean-sidecar/runs/rec_path_runid/reconcile")

        body = r.json()
        assert body["run_id"] == "rec_path_runid"
        assert body["algorithm_id"] == "SomeOtherAlgo"
        # Specifically: run_id must not have been silently shadowed.
        assert body["run_id"] != "SomeOtherAlgo"

    async def test_commission_drift_surfaces_in_divergences(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
        stub_normalized_result,
    ) -> None:
        """Default-brokerage run with $5 fee where IBKR expects $1 — the
        kind of report a non-reconciliation-grade run will produce."""
        ws = resolve_workspace("rec_drift", patched_artifacts_root)
        ws.ensure_layout()
        stub_normalized_result([self._filled_event(order_fee_amount=5.00)])

        r = await client.post("/api/lean-sidecar/runs/rec_drift/reconcile")

        body = r.json()
        assert body["divergent_count"] == 1
        d = body["divergences"][0]
        assert d["category"] == "commission_drift"
        assert d["recorded_fee"] == "5.00"
        assert d["expected_ibkr_fee"] == "1.00"
        assert d["delta"] == "4.00"

    async def test_404_when_workspace_missing(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        r = await client.post("/api/lean-sidecar/runs/rec_no_workspace/reconcile")
        assert r.status_code == 404
        assert r.json()["detail"]["reason"] == "run_not_found"

    async def test_404_when_normalized_missing(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A workspace that exists but where LEAN crashed before
        writing artifacts — the reconciler must 404 with the
        normalized_missing reason so the caller can branch."""
        from app.lean_sidecar.normalized_parser import NormalizedParserError
        from app.routers import lean_sidecar as router_module

        ws = resolve_workspace("rec_no_artifacts", patched_artifacts_root)
        ws.ensure_layout()

        def _raise(workspace):
            raise NormalizedParserError("summary file not found")

        monkeypatch.setattr(router_module, "parse_workspace", _raise)

        r = await client.post("/api/lean-sidecar/runs/rec_no_artifacts/reconcile")
        assert r.status_code == 404
        assert r.json()["detail"]["reason"] == "normalized_missing"

    async def test_invalid_run_id_rejected_at_reconcile(self, client: AsyncClient) -> None:
        r = await client.post("/api/lean-sidecar/runs/..escape/reconcile")
        assert r.status_code == 400
