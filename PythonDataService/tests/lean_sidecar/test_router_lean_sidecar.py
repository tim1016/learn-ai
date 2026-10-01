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
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

if TYPE_CHECKING:
    pass

from app.config import settings
from app.lean_sidecar import config as sidecar_config
from app.lean_sidecar.launcher.models import (
    LAUNCHER_CAPABILITY_READ_ONLY_WORKSPACE_DATA,
    LaunchResponse,
)
from app.lean_sidecar.launcher_client import DEFAULT_LAUNCHER_URL
from app.lean_sidecar.workspace import resolve_workspace
from app.main import app
from app.research.backtest_runs.service import SaveOutcome

pytestmark = pytest.mark.asyncio


PINNED_DIGEST_FOR_TESTS = "sha256:00000000000000000000000000000000000000000000000000000000cafebabe"
_TEST_BACKEND_URL = "http://test-backend"


@pytest.fixture(autouse=True)
def _isolated_launcher_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear ``LEAN_LAUNCHER_URL`` so ``respx.mock(base_url=DEFAULT_LAUNCHER_URL)``
    intercepts the launcher's HTTP traffic.

    Compose sets ``LEAN_LAUNCHER_URL=http://host.docker.internal:8090``
    on the live data-plane container. If that env leaks into pytest,
    the launcher_client posts to the live URL, respx doesn't intercept,
    and every router test that mocks the launcher fails with
    ``AllMockedAssertionError``. Autouse so individual tests don't
    have to remember to opt in.

    Also pins ``settings.BACKEND_URL`` to ``_TEST_BACKEND_URL`` so the
    jobs-surface calls go to the same predictable host regardless of
    which compose environment the container was started under, and
    stands in for the run repository write so no test reaches a database.
    """
    monkeypatch.delenv("LEAN_LAUNCHER_URL", raising=False)
    monkeypatch.delenv("LEAN_LAUNCHER_TOKEN", raising=False)
    monkeypatch.setattr(settings, "BACKEND_URL", _TEST_BACKEND_URL)

    async def _persisted(payload: dict) -> SaveOutcome:
        return SaveOutcome(status="saved", run_id=12345)

    from app.services import lean_sidecar_service

    monkeypatch.setattr(lean_sidecar_service, "persist_run_payload", _persisted)


@pytest.fixture
def patched_pin(monkeypatch: pytest.MonkeyPatch) -> str:
    """Pin a dummy image digest into config so the service does not
    refuse to launch for "no PINNED_LEAN_IMAGE_DIGEST" reasons."""
    from app.lean_sidecar.config import LeanRuntimeProvenance
    from app.services import lean_sidecar_service

    test_runtime_provenance = LeanRuntimeProvenance(
        image_digest=PINNED_DIGEST_FOR_TESTS,
        upstream_image_digest="sha256:" + "0" * 64,
        lean_version="test",
        source_commit="0" * 40,
        source_link_url_template="https://example.test/lean/*",
        binary_sha256={},
        pdb_sha256={},
    )
    monkeypatch.setattr(sidecar_config, "PINNED_LEAN_IMAGE_DIGEST", PINNED_DIGEST_FOR_TESTS)
    monkeypatch.setattr(
        sidecar_config,
        "ALLOWED_IMAGE_DIGESTS",
        frozenset({PINNED_DIGEST_FOR_TESTS}),
    )
    # Service reads PINNED_LEAN_IMAGE_DIGEST at module-import time
    # too; patch in-place.
    monkeypatch.setattr(lean_sidecar_service, "PINNED_LEAN_IMAGE_DIGEST", PINNED_DIGEST_FOR_TESTS)
    monkeypatch.setattr(
        lean_sidecar_service,
        "runtime_provenance_for_digest",
        lambda digest: test_runtime_provenance if digest == PINNED_DIGEST_FOR_TESTS else None,
    )
    return PINNED_DIGEST_FOR_TESTS


@pytest.fixture
def patched_artifacts_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the service's artifacts root into a tmp dir per test
    so concurrent tests don't collide on workspace dirs."""
    root = (tmp_path / "artifacts").resolve()
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sidecar_config, "DEFAULT_ARTIFACTS_ROOT", root)
    from app.services import lean_sidecar_service

    monkeypatch.setattr(lean_sidecar_service, "DEFAULT_ARTIFACTS_ROOT", root)
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


# P2.5 contract — start_ms_utc / end_ms_utc are 09:30 ET (session-open)
# millis, half-open. The trusted-sample window is Mon 2025-01-06 →
# Fri 2025-01-10 (5 trading days), so:
#   start_ms_utc = 09:30 ET of 2025-01-06 = 14:30 UTC = 1_736_173_800_000
#   end_ms_utc   = 09:30 ET of next_trading_day(2025-01-10)
#                = 09:30 ET of 2025-01-13 (Mon, no MLK)
#                = 14:30 UTC = 1_736_778_600_000
# Pre-P2.5 callers sent midnight-UTC ms; that contract is now rejected
# by the validator (ADR 0022 (a): a trading date is its session-open instant).
_GOOD_START_MS = 1_736_173_800_000
_GOOD_END_MS = 1_736_778_600_000


def _good_payload(run_id: str = "router_unit") -> dict:
    # PR B (Task 1.6): the legacy shape is still accepted for one
    # deprecation cycle. PR A's defaults for ``data_source`` /
    # ``bar_minutes`` / ``session`` / ``adjustment`` lived on the
    # Pydantic model; PR B requires all four to be explicit when a
    # caller uses the legacy shape so the router can tell the two
    # shapes apart.
    return {
        "run_id": run_id,
        "symbol": "SPY",
        "start_ms_utc": _GOOD_START_MS,
        "end_ms_utc": _GOOD_END_MS,
        "starting_cash": 100000.0,
        "data_source": "synthetic",
        "bar_minutes": 15,
        "session": "regular",
        "adjustment": "raw",
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


def _mock_launcher_healthz(mock: respx.MockRouter) -> None:
    """Mock the ``GET /healthz`` capability probe the service sends before
    every launch (:func:`assert_launcher_supports`, read-only workspace data).

    A launcher build that predates the ``capabilities`` field reads as
    "supports nothing optional" and the run is refused before staging, so
    every launch-mocking test must also answer this probe.
    """
    mock.get("/healthz").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "capabilities": [LAUNCHER_CAPABILITY_READ_ONLY_WORKSPACE_DATA],
            },
        )
    )


class TestCalendarNextTradingDayOpenEndpoint:
    """Per ADR 0022 (a), the
    half-open window's exclusive ``end_ms_utc`` is the 09:30 ET session
    open of the trading day *after* the operator's chosen end date. The
    frontend calls this endpoint so the unified Engine Lab's LEAN
    submission doesn't reproduce NYSE-calendar logic in TypeScript.
    """

    async def test_skips_weekend(self, client: AsyncClient) -> None:
        # Fri 2025-01-17 → Mon 2025-01-20 is MLK Day (closed), so the
        # next session is Tue 2025-01-21.
        # 09:30 ET on 2025-01-21 (EST = UTC-5) = 14:30 UTC.
        r = await client.get(
            "/api/lean-sidecar/calendar/next-trading-day-open",
            params={"date": "2025-01-17"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["next_trading_date"] == "2025-01-21"
        from datetime import UTC, datetime

        expected_ms = int(datetime(2025, 1, 21, 14, 30, tzinfo=UTC).timestamp() * 1000)
        assert body["session_open_ms_utc"] == expected_ms

    async def test_after_regular_trading_day(self, client: AsyncClient) -> None:
        # Mon 2025-01-13 → Tue 2025-01-14, both normal sessions.
        r = await client.get(
            "/api/lean-sidecar/calendar/next-trading-day-open",
            params={"date": "2025-01-13"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["next_trading_date"] == "2025-01-14"
        from datetime import UTC, datetime

        expected_ms = int(datetime(2025, 1, 14, 14, 30, tzinfo=UTC).timestamp() * 1000)
        assert body["session_open_ms_utc"] == expected_ms

    async def test_dst_awareness(self, client: AsyncClient) -> None:
        # Summer date — 09:30 EDT = 13:30 UTC, not 14:30. Mon 2025-07-14
        # → Tue 2025-07-15 (both normal sessions).
        r = await client.get(
            "/api/lean-sidecar/calendar/next-trading-day-open",
            params={"date": "2025-07-14"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["next_trading_date"] == "2025-07-15"
        from datetime import UTC, datetime

        expected_ms = int(datetime(2025, 7, 15, 13, 30, tzinfo=UTC).timestamp() * 1000)
        assert body["session_open_ms_utc"] == expected_ms


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

    async def test_oversized_window_rejected(self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """A window with more than _MAX_TRADING_DAYS trading days must be
        rejected — under the P2.5 contract, count is over
        [start_date, exclusive_end_date) sessions, not calendar days.

        The production cap is sized to the Polygon.io Starter plan's
        ~2-year minute-bar history (504 sessions). Test monkey-patches
        the cap down to a small value so the validator can be
        exercised without requiring a long fixture window.
        """
        from app.lean_sidecar.trading_calendar import session_open_ms_utc
        from app.routers import lean_sidecar as router_mod

        # Shrink the cap to 5 so a clean 2-week window trips it.
        monkeypatch.setattr(router_mod, "_MAX_TRADING_DAYS", 5)
        payload = _good_payload()
        payload["start_ms_utc"] = session_open_ms_utc(date(2025, 1, 6))
        payload["end_ms_utc"] = session_open_ms_utc(date(2025, 1, 21))
        r = await client.post("/api/lean-sidecar/trusted-runs", json=payload)
        assert r.status_code == 422
        assert "trading days" in r.text

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

    async def test_rejects_start_ms_not_session_open(self, client: AsyncClient) -> None:
        """P2.5 — start_ms_utc must be exactly 09:30 ET of a trading
        day. Midnight-UTC payloads (the pre-P2.5 contract) are
        rejected with a clear error pointing at the new contract."""
        payload = _good_payload()
        # 2025-01-06 00:00 UTC = the OLD-contract midnight-UTC value.
        payload["start_ms_utc"] = 1_736_121_600_000
        r = await client.post("/api/lean-sidecar/trusted-runs", json=payload)
        assert r.status_code == 422
        assert "09:30" in r.text or "session" in r.text.lower()

    async def test_rejects_end_ms_not_session_open(self, client: AsyncClient) -> None:
        payload = _good_payload()
        payload["end_ms_utc"] = 1_736_467_200_000  # midnight UTC of 2025-01-10
        r = await client.post("/api/lean-sidecar/trusted-runs", json=payload)
        assert r.status_code == 422
        assert "09:30" in r.text or "session" in r.text.lower()

    async def test_rejects_window_starting_on_weekend(self, client: AsyncClient) -> None:
        """P2.5 — start_date must be a trading day. Saturday/Sunday
        rejected with a message that names the offending date so an
        operator can fix the payload without reading the source."""
        from app.lean_sidecar.trading_calendar import session_open_ms_utc

        payload = _good_payload()
        # 2025-01-11 is a Saturday.
        payload["start_ms_utc"] = session_open_ms_utc(date(2025, 1, 11))
        payload["end_ms_utc"] = session_open_ms_utc(date(2025, 1, 14))  # Tue
        r = await client.post("/api/lean-sidecar/trusted-runs", json=payload)
        assert r.status_code == 422
        assert "2025-01-11" in r.text

    async def test_accepts_holiday_in_middle_of_window(self) -> None:
        """P2.5 — weekends and federal holidays IN BETWEEN trading-day
        endpoints are allowed; staging skips them. Schema-only — no
        podman/LEAN dependency."""
        from app.lean_sidecar.trading_calendar import session_open_ms_utc
        from app.routers.lean_sidecar import TrustedRunRequestModel

        payload = _good_payload()
        # MLK Day 2025 = Mon 2025-01-20. Range Fri 01-17 → Wed 01-22
        # (exclusive end) has MLK Monday in the middle as a holiday.
        payload["start_ms_utc"] = session_open_ms_utc(date(2025, 1, 17))
        payload["end_ms_utc"] = session_open_ms_utc(date(2025, 1, 22))
        model = TrustedRunRequestModel.model_validate(payload)
        assert model.start_ms_utc == payload["start_ms_utc"]

    async def test_accepts_window_touching_half_day(self) -> None:
        """Regression: comparison windows routinely cross NYSE early
        closes. Half-days are sessions, not blockers."""
        from app.lean_sidecar.trading_calendar import session_open_ms_utc
        from app.routers.lean_sidecar import TrustedRunRequestModel

        payload = _good_payload()
        # Wed 2025-11-26 → next_trading_day after Fri 2025-11-28.
        # 2025-11-27 is Thanksgiving (full holiday) AND 2025-11-28
        # is the Black-Friday early close.
        payload["start_ms_utc"] = session_open_ms_utc(date(2025, 11, 26))
        payload["end_ms_utc"] = session_open_ms_utc(date(2025, 12, 1))  # next_trading_day(11-28)
        model = TrustedRunRequestModel.model_validate(payload)
        assert model.start_ms_utc == payload["start_ms_utc"]
        assert model.end_ms_utc == payload["end_ms_utc"]

    async def test_accepts_user_two_year_window_crossing_2024_black_friday(self) -> None:
        """Regression for the LEAN Lab payload that previously failed
        with `window contains early-close day 2024-11-29`."""
        from app.routers.lean_sidecar import TrustedRunRequestModel

        payload = _good_payload()
        payload["run_id"] = "ui_run_20260519021742204_8wj5r"
        payload["start_ms_utc"] = 1_722_519_000_000
        payload["end_ms_utc"] = 1_779_111_000_000
        model = TrustedRunRequestModel.model_validate(payload)
        assert model.start_ms_utc == 1_722_519_000_000
        assert model.end_ms_utc == 1_779_111_000_000

    async def test_accepts_dst_straddling_window(self) -> None:
        """P2.5 — a window that straddles a DST boundary must validate
        cleanly when both endpoints resolve through the NY zone. DST
        starts 2025-03-09 EST→EDT."""
        from app.lean_sidecar.trading_calendar import session_open_ms_utc
        from app.routers.lean_sidecar import TrustedRunRequestModel

        payload = _good_payload()
        # Fri 2025-03-07 (EST) → Tue 2025-03-11 (EDT).
        payload["start_ms_utc"] = session_open_ms_utc(date(2025, 3, 7))
        payload["end_ms_utc"] = session_open_ms_utc(date(2025, 3, 12))  # next_trading_day(03-11)
        # Schema-only — no podman/LEAN dependency.
        model = TrustedRunRequestModel.model_validate(payload)
        assert model.start_ms_utc == payload["start_ms_utc"]
        assert model.end_ms_utc == payload["end_ms_utc"]

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
            _mock_launcher_healthz(mock)
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
            _mock_launcher_healthz(mock)
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
            _mock_launcher_healthz(mock)
            mock.post("/launch").mock(side_effect=httpx.ConnectError("refused"))
            r = await client.post(
                "/api/lean-sidecar/trusted-runs",
                json=_good_payload("router_unreach"),
            )
        assert r.status_code == 503
        assert r.json()["detail"]["reason"] == "launcher_unreachable"

    async def test_launcher_rejection_writes_failure_manifest(
        self,
        client: AsyncClient,
        patched_pin: str,
        patched_artifacts_root: Path,
        stub_image_extract: None,
        stub_normalized_parser: None,
    ) -> None:
        """Review-fix (P1.3): when the launcher rejects after the
        container has done work (e.g., ``workspace_max_mb_exceeded``),
        the orchestrator now writes a failure manifest with every
        staged hash + a ``failure_reason=<reason>`` note. Without
        this, the run would leave a fully-staged workspace on disk
        with no manifest, no sidebar entry, and no rejection audit
        trail."""
        async with respx.mock(base_url=DEFAULT_LAUNCHER_URL) as mock:
            _mock_launcher_healthz(mock)
            mock.post("/launch").mock(
                return_value=httpx.Response(
                    400,
                    json={
                        "detail": {
                            "reason": "workspace_max_mb_exceeded",
                            "message": "workspace 70MB > cap 64MB",
                        }
                    },
                )
            )
            r = await client.post(
                "/api/lean-sidecar/trusted-runs",
                json=_good_payload("router_failmanifest"),
            )
        # Router mapping unchanged: LauncherRejected → 400.
        assert r.status_code == 400
        assert r.json()["detail"]["reason"] == "workspace_max_mb_exceeded"
        # NEW behavior: failure manifest is on disk with the rejection
        # reason recorded.
        ws = resolve_workspace("router_failmanifest", patched_artifacts_root)
        assert ws.manifest_path.exists(), "failure manifest should be written even when launcher rejects"
        manifest = json.loads(ws.manifest_path.read_text(encoding="utf-8"))
        assert manifest["run_id"] == "router_failmanifest"
        assert manifest["exit_code"] is None
        notes = manifest.get("notes", [])
        assert any("failure_reason=" in n for n in notes), f"manifest notes lack failure_reason: {notes}"
        assert any("LauncherRejected" in n for n in notes), f"manifest notes should name the exception type: {notes}"
        # is_clean=False — failure path must NEVER paint as clean.
        assert any("is_clean=False" in n for n in notes)

    async def test_launcher_unreachable_writes_failure_manifest(
        self,
        client: AsyncClient,
        patched_pin: str,
        patched_artifacts_root: Path,
        stub_image_extract: None,
        stub_normalized_parser: None,
    ) -> None:
        """Same as above for the unreachable-launcher path: the manifest
        is written even when no LaunchResponse comes back. This is the
        most common silent-failure path historically — launcher down,
        operator restarts compose, no audit of the staged work."""
        async with respx.mock(base_url=DEFAULT_LAUNCHER_URL) as mock:
            _mock_launcher_healthz(mock)
            mock.post("/launch").mock(side_effect=httpx.ConnectError("refused"))
            r = await client.post(
                "/api/lean-sidecar/trusted-runs",
                json=_good_payload("router_unreach_manifest"),
            )
        assert r.status_code == 503
        ws = resolve_workspace("router_unreach_manifest", patched_artifacts_root)
        assert ws.manifest_path.exists()
        manifest = json.loads(ws.manifest_path.read_text(encoding="utf-8"))
        notes = manifest.get("notes", [])
        assert any("LauncherUnreachable" in n for n in notes), (
            f"manifest notes should name the unreachable exception: {notes}"
        )

    async def test_reused_run_id_returns_409(
        self,
        client: AsyncClient,
        patched_pin: str,
        patched_artifacts_root: Path,
        stub_image_extract: None,
        stub_normalized_parser: None,
    ) -> None:
        """Review-fix (P1.2): reusing a ``run_id`` would have let the
        new run inherit ``output/``, ``normalized/``, and
        ``manifest.json`` from the previous run. The orchestrator now
        rejects with HTTP 409 ``run_id_already_used`` before any
        staging touches the workspace; the operator must pick a fresh
        slug. The UI's default ``runId`` regenerates on every submit,
        so a 409 here means the slug was hand-edited to a used value."""
        payload = _good_payload("router_reused")
        async with respx.mock(base_url=DEFAULT_LAUNCHER_URL) as mock:
            _mock_launcher_healthz(mock)
            mock.post("/launch").mock(
                return_value=httpx.Response(200, json=_launcher_success_body("router_reused")),
            )
            first = await client.post("/api/lean-sidecar/trusted-runs", json=payload)
            assert first.status_code == 200, first.text
            # Re-submit identical payload — the workspace now exists.
            second = await client.post("/api/lean-sidecar/trusted-runs", json=payload)
        assert second.status_code == 409
        body = second.json()["detail"]
        assert body["reason"] == "run_id_already_used"
        assert "router_reused" in body["message"]
        # The previously-written manifest must NOT have been
        # overwritten — defense against future regressions where the
        # rejection happens too late.
        ws = resolve_workspace("router_reused", patched_artifacts_root)
        assert ws.manifest_path.exists()


class TestTemplateSelection:
    """Phase 5b — pydantic-layer template field defaults + validation."""

    async def test_template_defaults_to_trusted_default(self) -> None:
        """The field must default to ``trusted_default`` so existing
        callers (Phase 4a/c clients without the new field) keep the
        Phase-1 LEAN-default-brokerage behavior."""
        from app.routers.lean_sidecar import TrustedRunRequestModel

        payload = _good_payload()
        assert "template" not in payload
        model = TrustedRunRequestModel.model_validate(payload)
        assert model.template == "trusted_default"

    async def test_template_accepts_reconciliation(self) -> None:
        from app.routers.lean_sidecar import TrustedRunRequestModel

        payload = _good_payload()
        payload["template"] = "reconciliation"
        model = TrustedRunRequestModel.model_validate(payload)
        assert model.template == "reconciliation"

    async def test_template_accepts_deployment_validation(self) -> None:
        from app.routers.lean_sidecar import TrustedRunRequestModel

        payload = _good_payload()
        payload["template"] = "deployment_validation"
        model = TrustedRunRequestModel.model_validate(payload)
        assert model.template == "deployment_validation"

    async def test_template_accepts_ema_crossover_signal(self) -> None:
        from app.routers.lean_sidecar import TrustedRunRequestModel

        payload = _good_payload()
        payload["template"] = "ema_crossover_signal"

        model = TrustedRunRequestModel.model_validate(payload)

        assert model.template == "ema_crossover_signal"

    async def test_two_bps_template_defaults_its_strategy_parameters(self) -> None:
        from app.routers.lean_sidecar import TrustedRunRequestModel

        payload = _good_payload()
        payload["template"] = "ema_crossover_2_bps"

        model = TrustedRunRequestModel.model_validate(payload)

        assert model.strategy_parameters is not None
        assert model.strategy_parameters.model_dump() == {
            "gap_bps": 2.0,
            "rsi_min": 50.0,
            "rsi_max": 70.0,
        }

    async def test_two_bps_template_accepts_valid_strategy_parameters(self) -> None:
        from app.routers.lean_sidecar import TrustedRunRequestModel

        payload = _good_payload()
        payload["template"] = "ema_crossover_2_bps"
        payload["strategy_parameters"] = {
            "gap_bps": 4,
            "rsi_min": 52,
            "rsi_max": 68,
        }

        model = TrustedRunRequestModel.model_validate(payload)

        assert model.strategy_parameters is not None
        assert model.strategy_parameters.gap_bps == 4

    @pytest.mark.parametrize(
        "strategy_parameters",
        [
            {"gap_bps": -1, "rsi_min": 50, "rsi_max": 70},
            {"gap_bps": 2, "rsi_min": 70, "rsi_max": 50},
            {"gap_bps": 2, "rsi_min": 50, "rsi_max": 70, "unknown": 1},
        ],
    )
    async def test_two_bps_template_rejects_invalid_strategy_parameters(
        self,
        strategy_parameters: dict[str, float],
    ) -> None:
        from pydantic import ValidationError

        from app.routers.lean_sidecar import TrustedRunRequestModel

        payload = _good_payload()
        payload["template"] = "ema_crossover_2_bps"
        payload["strategy_parameters"] = strategy_parameters

        with pytest.raises(ValidationError):
            TrustedRunRequestModel.model_validate(payload)

    async def test_signal_template_accepts_all_resolved_strategy_parameters(self) -> None:
        from app.routers.lean_sidecar import TrustedRunRequestModel

        payload = _good_payload()
        payload["template"] = "ema_crossover_signal"
        payload["strategy_parameters"] = {
            "gap": 0.35,
            "gap_bps": 2.5,
            "rsi_min": 42,
            "rsi_max": 68,
        }

        model = TrustedRunRequestModel.model_validate(payload)

        assert model.strategy_parameters is not None
        assert model.strategy_parameters.model_dump() == payload["strategy_parameters"]

    @pytest.mark.parametrize(
        ("strategy_parameters", "expected_rsi_min"),
        [({}, 50.0), ({"rsi_min": 45}, 45.0)],
    )
    async def test_signal_template_partial_parameters_use_signal_defaults(
        self,
        strategy_parameters: dict[str, float],
        expected_rsi_min: float,
    ) -> None:
        from app.routers.lean_sidecar import TrustedRunRequestModel

        payload = _good_payload()
        payload["template"] = "ema_crossover_signal"
        payload["strategy_parameters"] = strategy_parameters

        model = TrustedRunRequestModel.model_validate(payload)

        assert model.strategy_parameters is not None
        assert model.strategy_parameters.gap_bps == 0.0
        assert model.strategy_parameters.rsi_min == expected_rsi_min

    async def test_non_parameterized_template_rejects_strategy_parameters(self) -> None:
        from pydantic import ValidationError

        from app.routers.lean_sidecar import TrustedRunRequestModel

        payload = _good_payload()
        payload["template"] = "rsi_mean_reversion"
        payload["strategy_parameters"] = {
            "gap_bps": 2,
            "rsi_min": 50,
            "rsi_max": 70,
        }

        with pytest.raises(ValidationError, match="does not accept strategy_parameters"):
            TrustedRunRequestModel.model_validate(payload)

    async def test_template_rejects_unknown_value(self, client: AsyncClient) -> None:
        """A typo or unknown template must 422 — silently falling
        through to the default would mask brokerage intent."""
        payload = _good_payload()
        payload["template"] = "not_a_real_template"
        r = await client.post("/api/lean-sidecar/trusted-runs", json=payload)
        assert r.status_code == 422


def test_trusted_run_request_model_accepts_legacy_top_level_shape() -> None:
    """PR B Task 1.6: legacy payload (symbol/data_source/bar_minutes/...) is
    accepted for one deprecation cycle and synthesizes a DataPolicy block."""
    from app.routers.lean_sidecar import TrustedRunRequestModel

    payload = {
        "run_id": "test-legacy-shape",
        "symbol": "SPY",
        "start_ms_utc": _GOOD_START_MS,
        "end_ms_utc": _GOOD_END_MS,
        "starting_cash": 100_000.0,
        "template": "ema_crossover",
        "data_source": "polygon",
        "bar_minutes": 15,
        "session": "regular",
        "adjustment": "raw",
    }
    model = TrustedRunRequestModel(**payload)
    assert model.data_policy is not None
    assert model.data_policy.symbol == "SPY"
    assert model.data_policy.session == "regular"
    assert model.data_policy.adjusted is False  # adjustment="raw" -> adjusted=False
    assert model.data_policy.strategy_bars.multiplier == 15


def test_trusted_run_request_model_accepts_new_data_policy_shape() -> None:
    """PR B Task 1.6: the canonical post-PR-B shape carries a ``data_policy`` block."""
    from app.routers.lean_sidecar import TrustedRunRequestModel

    payload = {
        "run_id": "test-new-shape",
        "start_ms_utc": _GOOD_START_MS,
        "end_ms_utc": _GOOD_END_MS,
        "starting_cash": 100_000.0,
        "template": "ema_crossover",
        "data_policy": {
            "source": "polygon",
            "symbol": "SPY",
            "adjusted": True,
            "session": "regular",
            "input_bars": {"timespan": "minute", "multiplier": 1},
            "strategy_bars": {"timespan": "minute", "multiplier": 15},
            "timestamp_policy": "bar_close_ms_utc",
            "timezone": "America/New_York",
            "provider_kind": "live",
            "fixture_id": None,
            "fixture_sha256": None,
        },
    }
    model = TrustedRunRequestModel(**payload)
    assert model.data_policy.symbol == "SPY"
    assert model.data_policy.adjusted is True


def test_trusted_run_request_model_rejects_mixed_shape() -> None:
    """PR B Task 1.6: posting both legacy fields AND a ``data_policy`` block
    is a payload-construction bug — pick one shape."""
    from pydantic import ValidationError

    from app.routers.lean_sidecar import TrustedRunRequestModel

    with pytest.raises(ValidationError, match="data_policy"):
        TrustedRunRequestModel(
            run_id="test-mixed",
            symbol="SPY",
            start_ms_utc=_GOOD_START_MS,
            end_ms_utc=_GOOD_END_MS,
            starting_cash=100_000.0,
            data_policy={
                "source": "polygon",
                "symbol": "SPY",
                "adjusted": True,
                "session": "regular",
                "input_bars": {"timespan": "minute", "multiplier": 1},
                "strategy_bars": {"timespan": "minute", "multiplier": 15},
                "timestamp_policy": "bar_close_ms_utc",
                "timezone": "America/New_York",
                "provider_kind": "live",
                "fixture_id": None,
                "fixture_sha256": None,
            },
        )


def test_trusted_run_request_model_legacy_shape_defaults_adjustment_to_raw() -> None:
    """PR B Task 1.6: omitting ``adjustment`` on a LEGACY-shape payload
    synthesizes ``adjusted=False`` (the pre-PR-B wire vocabulary's
    implicit value was ``"raw"``). Silently switching legacy callers
    to ``adjusted=True`` would break the one-deprecation-cycle compat
    promise. New-shape callers carrying a ``data_policy`` block still
    default to ``adjusted=True`` via the field default on
    ``_DataPolicyModel``.
    """
    from app.routers.lean_sidecar import TrustedRunRequestModel

    payload = {
        "run_id": "test-default-adj",
        "symbol": "SPY",
        "start_ms_utc": _GOOD_START_MS,
        "end_ms_utc": _GOOD_END_MS,
        "starting_cash": 100_000.0,
        "template": "ema_crossover",
        "data_source": "polygon",
        "bar_minutes": 15,
        "session": "regular",
        # no "adjustment" key
    }
    model = TrustedRunRequestModel(**payload)
    assert model.data_policy.adjusted is False


def test_trusted_run_request_model_new_shape_defaults_adjusted_to_true() -> None:
    """PR B § 4.4: NEW-shape callers (carrying a ``data_policy`` block)
    that omit ``adjusted`` get the field default ``True`` — the
    pre-adjusted-staging default for the post-PR-B contract. This is
    distinct from the legacy-shape default, which preserves PR A's
    implicit ``raw`` behavior for one cycle.
    """
    from app.routers.lean_sidecar import TrustedRunRequestModel

    payload = {
        "run_id": "test-new-shape-default-adj",
        "start_ms_utc": _GOOD_START_MS,
        "end_ms_utc": _GOOD_END_MS,
        "starting_cash": 100_000.0,
        "template": "ema_crossover",
        "data_policy": {
            "source": "polygon",
            "symbol": "SPY",
            # no "adjusted" key — exercise the field default
            "session": "regular",
            "input_bars": {"timespan": "minute", "multiplier": 1},
            "strategy_bars": {"timespan": "minute", "multiplier": 15},
        },
    }
    model = TrustedRunRequestModel(**payload)
    assert model.data_policy.adjusted is True


def test_trusted_run_request_model_accepts_minimal_legacy_payload() -> None:
    """PR B Task 1.6 (P1 review): the existing Lean Lab UI posts only
    ``run_id``/``symbol``/window/cash/template — no ``data_source``,
    ``bar_minutes``, ``session``, or ``adjustment``. The one-cycle
    compat guarantee requires accepting this minimal shape and
    defaulting the missing legacy fields to PR A's defaults, NOT
    422-ing. Without this defaulting, the deployed UI would 422 on
    every submit until shipped to the new shape.
    """
    from app.routers.lean_sidecar import TrustedRunRequestModel

    payload = {
        "run_id": "test-minimal-legacy",
        "symbol": "SPY",
        "start_ms_utc": _GOOD_START_MS,
        "end_ms_utc": _GOOD_END_MS,
        "starting_cash": 100_000.0,
        "template": "ema_crossover",
        # NOTHING else — no data_source, bar_minutes, session, adjustment, data_policy
    }
    model = TrustedRunRequestModel(**payload)
    assert model.data_policy is not None
    assert model.data_policy.symbol == "SPY"
    assert model.data_policy.source == "synthetic"  # legacy default
    assert model.data_policy.session == "regular"  # legacy default
    assert model.data_policy.strategy_bars.multiplier == 15  # legacy default
    assert model.data_policy.adjusted is False  # legacy "raw" -> False


def test_trusted_run_request_model_rejects_minimal_legacy_payload_without_symbol() -> None:
    """``symbol`` has no sensible default — it's the asset being traded.
    Omitting it on a legacy-shape payload still 422s.
    """
    from pydantic import ValidationError

    from app.routers.lean_sidecar import TrustedRunRequestModel

    payload = {
        "run_id": "test-missing-symbol",
        # no "symbol"
        "start_ms_utc": _GOOD_START_MS,
        "end_ms_utc": _GOOD_END_MS,
        "starting_cash": 100_000.0,
        "template": "ema_crossover",
    }
    with pytest.raises(ValidationError, match="symbol"):
        TrustedRunRequestModel(**payload)
