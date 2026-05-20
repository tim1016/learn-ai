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
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

if TYPE_CHECKING:
    from app.lean_sidecar.normalized_parser import NormalizedResult

from app.config import settings
from app.lean_sidecar import config as sidecar_config
from app.lean_sidecar.launcher.models import LaunchResponse
from app.lean_sidecar.launcher_client import DEFAULT_LAUNCHER_URL
from app.lean_sidecar.workspace import resolve_workspace
from app.main import app

pytestmark = pytest.mark.asyncio


PINNED_DIGEST_FOR_TESTS = "sha256:00000000000000000000000000000000000000000000000000000000cafebabe"
_TEST_BACKEND_URL = "http://test-backend"
_PERSIST_LEAN_URL = f"{_TEST_BACKEND_URL}/api/backtest-runs/persist-lean"


@pytest.fixture(autouse=True)
def _isolated_launcher_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear ``LEAN_LAUNCHER_URL`` so ``respx.mock(base_url=DEFAULT_LAUNCHER_URL)``
    intercepts the launcher's HTTP traffic.

    Compose now sets ``LEAN_LAUNCHER_URL=http://172.23.176.1:8090`` on
    the live data-plane container (host-process launcher reachable via
    the WSL2 adapter IP). If that env leaks into pytest, the
    launcher_client posts to the live URL, respx doesn't intercept,
    and every router test that mocks the launcher fails with
    ``AllMockedAssertionError``. Autouse so individual tests don't
    have to remember to opt in.

    Also pins ``settings.BACKEND_URL`` to ``_TEST_BACKEND_URL`` so the
    persist-lean calls go to the same predictable host regardless of
    which compose environment the container was started under.
    """
    monkeypatch.delenv("LEAN_LAUNCHER_URL", raising=False)
    monkeypatch.delenv("LEAN_LAUNCHER_TOKEN", raising=False)
    monkeypatch.setattr(settings, "BACKEND_URL", _TEST_BACKEND_URL)


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


# P2.5 contract — start_ms_utc / end_ms_utc are 09:30 ET (session-open)
# millis, half-open. The trusted-sample window is Mon 2025-01-06 →
# Fri 2025-01-10 (5 trading days), so:
#   start_ms_utc = 09:30 ET of 2025-01-06 = 14:30 UTC = 1_736_173_800_000
#   end_ms_utc   = 09:30 ET of next_trading_day(2025-01-10)
#                = 09:30 ET of 2025-01-13 (Mon, no MLK)
#                = 14:30 UTC = 1_736_778_600_000
# Pre-P2.5 callers sent midnight-UTC ms; that contract is now rejected
# by the validator. See docs/handoffs/2026-05-18-design-p2-5-date-semantics-v2.md.
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


class TestCalendarBlockedDatesEndpoint:
    """P2.5 calendar primitive: the UI fetches weekends and holidays
    in a date range so the date picker can
    disable + label each blocked date without re-implementing the
    calendar client-side."""

    async def test_returns_blocked_dates_with_reasons(self, client: AsyncClient) -> None:
        r = await client.get(
            "/api/lean-sidecar/calendar/blocked-dates",
            params={"from": "2026-11-23", "to": "2026-11-30"},
        )
        assert r.status_code == 200
        data = r.json()
        # 2026-11-26 = Thanksgiving (holiday). 2026-11-27 =
        # Black Friday early close, but still a trading session.
        # Weekends 11-28, 11-29 = weekend.
        by_date = {entry["date"]: entry["reason"] for entry in data["blocked"]}
        assert by_date["2026-11-26"] == "holiday"
        assert "2026-11-27" not in by_date
        assert by_date["2026-11-28"] == "weekend"
        assert by_date["2026-11-29"] == "weekend"
        # Regular trading days are NOT in the payload.
        assert "2026-11-23" not in by_date

    async def test_rejects_end_before_start(self, client: AsyncClient) -> None:
        r = await client.get(
            "/api/lean-sidecar/calendar/blocked-dates",
            params={"from": "2026-01-10", "to": "2026-01-06"},
        )
        assert r.status_code == 422
        assert "after" in r.text.lower() or "before" in r.text.lower()

    async def test_caps_range_to_prevent_abuse(self, client: AsyncClient) -> None:
        """Single request returns ≤ ~2 years of dates so a buggy UI
        loop can't degrade the launcher with a 100-year sweep."""
        r = await client.get(
            "/api/lean-sidecar/calendar/blocked-dates",
            params={"from": "2026-01-01", "to": "2099-01-01"},
        )
        assert r.status_code == 422
        assert "range" in r.text.lower() or "max" in r.text.lower()


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
            mock.post("/launch").mock(return_value=httpx.Response(200, json=_launcher_success_body("router_happy")))
            mock.post(_PERSIST_LEAN_URL).mock(return_value=httpx.Response(200, json={"strategy_execution_id": 12345}))
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
            mock.post("/launch").mock(
                return_value=httpx.Response(200, json=_launcher_success_body("router_reused")),
            )
            mock.post(_PERSIST_LEAN_URL).mock(return_value=httpx.Response(200, json={"strategy_execution_id": 12345}))
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
            mock.post(_PERSIST_LEAN_URL).mock(return_value=httpx.Response(200, json={"strategy_execution_id": 12345}))
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
    lean_error_categories: list[str] | None = None,
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
    if lean_error_categories is not None:
        # Mirror the service's str(sorted([...])) format so the parser
        # sees real-shape input.
        notes.append(f"lean_error_categories={sorted(lean_error_categories)}")
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

    async def test_scan_hard_cap_sorts_before_slicing(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Review-fix (P2.7): truncating to ``_SCAN_HARD_CAP`` BEFORE
        sorting could drop the newest runs once the artifacts root
        grows past the work cap, because filesystem ``iterdir()`` order
        is not guaranteed.

        With 4 candidate dirs and ``_SCAN_HARD_CAP=2``, the older lex
        order (``aaa < bbb < yyy < zzz``) would have made the bug
        invisible — both old code (sort lex first, then slice) and new
        code (sort lex first, then slice) keep the lexically-newest
        two. We instead use lexically-newest-but-actually-OLD ids: a
        modern run with a fresh timestamp prefix sorts lex-late and
        must reach the manifest-timestamp sort to surface at the top.

        Names ``ui_run_a/m/y/z`` deliberately put the genuinely-newest
        ``a`` lex-LAST so the sort-before-slice change matters: with
        cap=2, the lex sort keeps {y, z}, and the manifest-timestamp
        sort then surfaces them by started_at — confirming the cap is
        applied after the lex sort, not during the unstable iterdir
        order.
        """
        from app.routers import lean_sidecar as lean_sidecar_router

        monkeypatch.setattr(lean_sidecar_router, "_RUN_INDEX_CAP", 5)
        monkeypatch.setattr(lean_sidecar_router, "_SCAN_HARD_CAP", 2)
        _write_manifest(patched_artifacts_root, "ui_run_a", started_at_ms=4000)
        _write_manifest(patched_artifacts_root, "ui_run_m", started_at_ms=3000)
        _write_manifest(patched_artifacts_root, "ui_run_y", started_at_ms=2000)
        _write_manifest(patched_artifacts_root, "ui_run_z", started_at_ms=1000)

        body = (await client.get("/api/lean-sidecar/runs")).json()
        ids = [row["run_id"] for row in body["runs"]]
        # Hard cap of 2 → lex sort keeps {z, y}. Manifest-timestamp
        # sort then orders them by started_at_ms desc.
        assert set(ids) == {"ui_run_z", "ui_run_y"}
        assert body["truncated"] is True

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

    async def test_lean_error_categories_parsed_from_note(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        """Phase 4f: the manifest's ``lean_error_categories=[...]`` note
        carries the bucket names from the launcher's classifier. The
        index must surface them so the sidebar can show WHICH category
        was hit when rehydrating a non-clean run."""
        _write_manifest(
            patched_artifacts_root,
            "ui_run_with_categories",
            exit_code=0,
            is_clean=False,
            lean_error_categories=["failed_data_requests", "runtime_error"],
        )
        row = (await client.get("/api/lean-sidecar/runs")).json()["runs"][0]
        # Sorted because the service serializes via sorted(...) and the
        # parser preserves order.
        assert row["lean_error_categories"] == ["failed_data_requests", "runtime_error"]

    async def test_lean_error_categories_empty_when_note_absent(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        """Pre-Phase-4f manifests don't have the categories note. The
        index must default to an empty list (not None) so the UI can
        branch on length without a null-check."""
        _write_manifest(patched_artifacts_root, "ui_run_legacy_no_cats")
        row = (await client.get("/api/lean-sidecar/runs")).json()["runs"][0]
        assert row["lean_error_categories"] == []

    async def test_lean_error_categories_filters_unknown_buckets(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        """A future LEAN version adding a new bucket name would land
        in the manifest before the launcher's classifier is taught
        about it. The parser must drop unknown bucket names rather
        than render arbitrary text into the sidebar."""
        from app.routers.lean_sidecar import _parse_categories_note

        out = _parse_categories_note("['failed_data_requests', 'mystery_bucket', 'runtime_error']")
        assert out == ["failed_data_requests", "runtime_error"]

    async def test_lean_error_categories_empty_list_note(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        """A clean run still writes the note (with an empty list).
        Must round-trip to ``[]``, not None."""
        _write_manifest(
            patched_artifacts_root,
            "ui_run_clean_explicit",
            is_clean=True,
            lean_error_categories=[],
        )
        row = (await client.get("/api/lean-sidecar/runs")).json()["runs"][0]
        assert row["lean_error_categories"] == []

    async def test_lean_error_categories_malformed_note_falls_back_empty(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        """A note shape we don't recognize (e.g., a manual edit
        breaking the bracketed format) must default to [], not crash
        the index endpoint."""
        from app.routers.lean_sidecar import _parse_categories_note

        assert _parse_categories_note("not a list") == []
        assert _parse_categories_note("") == []
        assert _parse_categories_note("[malformed") == []


class TestPostReconcileEndpoint:
    """Phase 5a — POST /runs/{id}/reconcile integration tests.

    Uses the same `_stub_parse_workspace` pattern as the inspection
    endpoints — we mock `parse_workspace` to return a stubbed
    `NormalizedResult` rather than write LEAN-shaped artifact files into
    the workspace, because the reconciler logic is what's under test
    here, not the parser."""

    @pytest.fixture
    def stub_normalized_result(self, patched_artifacts_root: Path) -> Callable[..., NormalizedResult]:
        """Factory that writes a ``result.json`` to the run's workspace.

        Reviewer P2: the reconcile endpoint reads the persisted
        ``result.json`` directly (not a fresh re-parse of LEAN's raw
        artifacts), so tests must arrange the file on disk rather than
        monkeypatch ``parse_workspace``. This factory writes a valid
        ``NormalizedResult`` document into ``<workspace>/normalized/
        result.json`` and returns it for assertions.
        """
        from app.lean_sidecar.normalized_parser import NormalizedResult

        def _make_factory(
            events: list[dict],
            *,
            algorithm_id: str = "MyAlgorithm",
            parser_version: str = "phase-3a-r1",
            run_id: str | None = None,
        ) -> NormalizedResult:
            from app.lean_sidecar.normalized_parser import NormalizedOrderEvent

            order_events = [NormalizedOrderEvent.model_validate(e) for e in events]
            result = NormalizedResult(
                parser_version=parser_version,
                algorithm_id=algorithm_id,
                statistics={},
                runtime_statistics={},
                equity_curve=[],
                order_events=order_events,
                total_order_events=len(order_events),
                total_equity_points=0,
            )
            # The most recently-written workspace's result.json wins
            # when run_id is omitted; callers that care about isolation
            # pass run_id explicitly.
            target_run_ids = [run_id] if run_id else [p.name for p in patched_artifacts_root.iterdir() if p.is_dir()]
            for rid in target_run_ids:
                ws = resolve_workspace(rid, patched_artifacts_root)
                ws.normalized_dir.mkdir(parents=True, exist_ok=True)
                (ws.normalized_dir / "result.json").write_text(
                    result.model_dump_json(),
                    encoding="utf-8",
                )
            return result

        return _make_factory

    def _filled_event(self, **overrides: object) -> dict:
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
        stub_normalized_result: Callable[..., NormalizedResult],
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
        stub_normalized_result: Callable[..., NormalizedResult],
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
        stub_normalized_result: Callable[..., NormalizedResult],
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
    ) -> None:
        """A workspace that exists but where LEAN crashed before
        producing parseable artifacts — the orchestrator never wrote
        ``result.json``. The reconciler must 404 with the
        ``normalized_missing`` reason so the caller can branch."""
        ws = resolve_workspace("rec_no_artifacts", patched_artifacts_root)
        ws.ensure_layout()
        # Intentionally do NOT write normalized_dir/result.json.

        r = await client.post("/api/lean-sidecar/runs/rec_no_artifacts/reconcile")
        assert r.status_code == 404
        assert r.json()["detail"]["reason"] == "normalized_missing"

    async def test_404_when_result_json_malformed(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        """A ``result.json`` that exists but fails NormalizedResult
        validation (e.g., truncated mid-write, or schema drift from a
        future parser version) must 404 with the same reason rather
        than 500 — surfaces as a recoverable "missing/unreadable"
        condition for the caller."""
        ws = resolve_workspace("rec_malformed_result", patched_artifacts_root)
        ws.ensure_layout()
        ws.normalized_dir.mkdir(parents=True, exist_ok=True)
        (ws.normalized_dir / "result.json").write_text(
            "{not valid json",
            encoding="utf-8",
        )

        r = await client.post("/api/lean-sidecar/runs/rec_malformed_result/reconcile")
        assert r.status_code == 404
        assert r.json()["detail"]["reason"] == "normalized_missing"

    async def test_parser_version_echoed_on_report(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
        stub_normalized_result: Callable[..., NormalizedResult],
    ) -> None:
        """Reviewer P2: the pinned ``parser_version`` from the persisted
        ``result.json`` is echoed back so a downstream consumer can tell
        whether two reconciliation reports are comparable."""
        ws = resolve_workspace("rec_parser_pin", patched_artifacts_root)
        ws.ensure_layout()
        stub_normalized_result(
            [self._filled_event(order_fee_amount=1.00)],
            parser_version="phase-3a-r1",
            run_id="rec_parser_pin",
        )

        r = await client.post("/api/lean-sidecar/runs/rec_parser_pin/reconcile")

        assert r.status_code == 200
        assert r.json()["normalized_parser_version"] == "phase-3a-r1"

    async def test_invalid_run_id_rejected_at_reconcile(self, client: AsyncClient) -> None:
        r = await client.post("/api/lean-sidecar/runs/..escape/reconcile")
        assert r.status_code == 400


def _write_valid_result_json(
    *,
    run_id: str,
    artifacts_root: Path,
    order_fee_amount: float = 1.00,
    parser_version: str = "phase-3a-r1",
    algorithm_id: str = "MyAlgorithm",
) -> None:
    """Materialize a minimal-but-valid ``normalized/result.json`` for a
    workspace. Module-level so both the Phase 5a and Phase 5g.1 test
    classes can share it without class-fixture lookup complications."""
    from app.lean_sidecar.normalized_parser import NormalizedOrderEvent, NormalizedResult

    event_dict = {
        "order_event_id": 1,
        "order_id": 100,
        "algorithm_id": algorithm_id,
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
        "order_fee_amount": order_fee_amount,
        "order_fee_currency": "USD",
        "message": None,
    }
    order_events = [NormalizedOrderEvent.model_validate(event_dict)]
    result = NormalizedResult(
        parser_version=parser_version,
        algorithm_id=algorithm_id,
        statistics={},
        runtime_statistics={},
        equity_curve=[],
        order_events=order_events,
        total_order_events=len(order_events),
        total_equity_points=0,
    )
    ws = resolve_workspace(run_id, artifacts_root)
    ws.normalized_dir.mkdir(parents=True, exist_ok=True)
    (ws.normalized_dir / "result.json").write_text(
        result.model_dump_json(),
        encoding="utf-8",
    )


def _write_cross_run_manifest(
    *,
    run_id: str,
    artifacts_root: Path,
    symbol: str = "SPY",
    start_date: str = "2025-01-06",
    end_date: str = "2025-01-06",
    starting_cash: str = "100000",
    brokerage_policy: str = "interactive_brokers",
) -> None:
    """Stage a minimal manifest.json the cross-reconcile endpoint can
    read for symbol/dates/cash. The shape matches what the real service
    persists (``parameters`` dict + ``requested_window_ms``) but only
    populates the fields the cross-runner extracts.

    ``brokerage_policy`` defaults to ``"interactive_brokers"`` so
    existing tests (which don't pin a policy explicitly) keep
    exercising the assert_fees=true path without 400ing on the new
    Open-Q2 guard.
    """
    import json as _json
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _Zi

    _et = _Zi("America/New_York")
    sy, sm, sd = (int(x) for x in start_date.split("-"))
    ey, em, ed = (int(x) for x in end_date.split("-"))
    start_ms = int(_dt(sy, sm, sd, tzinfo=_et).timestamp() * 1000)
    end_ms = int(_dt(ey, em, ed, 23, 59, 59, tzinfo=_et).timestamp() * 1000)
    ws = resolve_workspace(run_id, artifacts_root)
    ws.ensure_layout()
    ws.manifest_path.write_text(
        _json.dumps(
            {
                "parameters": {
                    "symbol": symbol,
                    "start_date": start_date,
                    "end_date": end_date,
                    "starting_cash": starting_cash,
                },
                "brokerage_policy": brokerage_policy,
                "requested_window_ms": {"start_ms": start_ms, "end_ms": end_ms},
                "bars_consumed_by_symbol": {symbol: 0},
            }
        ),
        encoding="utf-8",
    )


def _stage_minute_data(
    *,
    run_id: str,
    artifacts_root: Path,
    symbol: str = "SPY",
) -> None:
    """Stage one trading day of synthetic minute bars in the workspace
    so the Engine-Lab cross-runner has bars to iterate."""
    from datetime import date as _date
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    from decimal import Decimal
    from zoneinfo import ZoneInfo as _Zi

    from app.engine.data.trade_bar import TradeBar
    from app.lean_sidecar.staging import stage_minute_bars

    _et = _Zi("America/New_York")
    ws = resolve_workspace(run_id, artifacts_root)
    ws.ensure_layout()
    bars = []
    for i in range(10):
        start = _dt(2025, 1, 6, 9, 30 + i, tzinfo=_et)
        price = Decimal(100 + i)
        bars.append(
            TradeBar(
                symbol=symbol.upper(),
                time=start,
                end_time=start + _td(minutes=1),
                open=price,
                high=price + Decimal("0.5"),
                low=price - Decimal("0.5"),
                close=price + Decimal("0.25"),
                volume=10_000,
            )
        )
    stage_minute_bars(ws, symbol=symbol, bars_by_date=[(_date(2025, 1, 6), bars)])


class TestPostCrossReconcileEndpoint:
    """Phase 5g.3 — POST /runs/{id}/cross-reconcile end-to-end tests.

    Phase 5g.1 shipped the endpoint scaffold (501); Phase 5g.2 shipped
    the cross-run primitive; this PR wires them together and runs the
    diff. The tests below exercise the now-live happy path plus the
    new error branches (manifest_missing, manifest_incomplete,
    strategy_not_found, strategy_incompatible).
    """

    async def test_endpoint_runs_engine_and_returns_real_report(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        """Phase 5g.3 happy path: workspace fully staged, LEAN side has
        zero recorded fills (empty result.json), Engine-Lab buy-and-hold
        emits one Buy on the first staged bar. The report carries one
        DECISION_MISMATCH row (Engine has, LEAN doesn't), counted as
        gating."""
        run_id = "cross_real_report"
        ws = resolve_workspace(run_id, patched_artifacts_root)
        ws.ensure_layout()
        # LEAN side: zero fills.
        from app.lean_sidecar.normalized_parser import NormalizedResult

        empty_result = NormalizedResult(
            parser_version="phase-3a-r1",
            algorithm_id="MyAlgorithm",
            statistics={},
            runtime_statistics={},
            equity_curve=[],
            order_events=[],
            total_order_events=0,
            total_equity_points=0,
        )
        ws.normalized_dir.mkdir(parents=True, exist_ok=True)
        (ws.normalized_dir / "result.json").write_text(empty_result.model_dump_json(), encoding="utf-8")
        _write_cross_run_manifest(run_id=run_id, artifacts_root=patched_artifacts_root)
        _stage_minute_data(run_id=run_id, artifacts_root=patched_artifacts_root)

        r = await client.post(
            f"/api/lean-sidecar/runs/{run_id}/cross-reconcile",
            json={"engine_lab_strategy_class": "BuyAndHoldStrategy"},
        )

        assert r.status_code == 200, r.json()
        body = r.json()
        assert body["schema_version"] == 1
        assert body["run_id"] == run_id
        assert body["engine_lab_strategy_class"] == "BuyAndHoldStrategy"
        assert body["assert_fees"] is False
        assert body["lean_total_fills"] == 0
        assert body["engine_total_fills"] >= 1
        # One DECISION_MISMATCH (Engine has, LEAN doesn't).
        assert body["divergent_count"] >= 1
        assert body["counts_by_category"].get("decision_mismatch", 0) >= 1
        assert body["passed"] is False  # gating divergence present
        # First divergence row carries the engine_fill snapshot but no
        # lean_fill.
        first = body["divergences"][0]
        assert first["category"] == "decision_mismatch"
        assert first["lean_fill"] is None
        assert first["engine_fill"] is not None
        assert first["engine_fill"]["symbol"] == "SPY"
        assert first["engine_fill"]["side"] == "Buy"

    async def test_404_when_manifest_missing(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        """Workspace + result.json present but manifest.json absent —
        the cross-run cannot infer symbol/dates/cash without it."""
        run_id = "cross_no_manifest"
        ws = resolve_workspace(run_id, patched_artifacts_root)
        ws.ensure_layout()
        _write_valid_result_json(run_id=run_id, artifacts_root=patched_artifacts_root)
        # Deliberately DO NOT write manifest.json.

        r = await client.post(
            f"/api/lean-sidecar/runs/{run_id}/cross-reconcile",
            json={"engine_lab_strategy_class": "BuyAndHoldStrategy"},
        )
        assert r.status_code == 404
        assert r.json()["detail"]["reason"] == "manifest_missing"

    async def test_400_when_manifest_incomplete(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        """Manifest exists but lacks ``parameters.symbol`` AND has no
        single-symbol ``bars_consumed_by_symbol`` fallback. The endpoint
        surfaces the missing field explicitly."""
        import json as _json

        run_id = "cross_bad_manifest"
        ws = resolve_workspace(run_id, patched_artifacts_root)
        ws.ensure_layout()
        _write_valid_result_json(run_id=run_id, artifacts_root=patched_artifacts_root)
        # Manifest with no symbol AND empty bars_consumed_by_symbol.
        ws.manifest_path.write_text(
            _json.dumps(
                {
                    "parameters": {
                        "start_date": "2025-01-06",
                        "end_date": "2025-01-06",
                        "starting_cash": "100000",
                    },
                    "bars_consumed_by_symbol": {},
                }
            ),
            encoding="utf-8",
        )

        r = await client.post(
            f"/api/lean-sidecar/runs/{run_id}/cross-reconcile",
            json={"engine_lab_strategy_class": "BuyAndHoldStrategy"},
        )
        assert r.status_code == 400
        body = r.json()["detail"]
        assert body["reason"] == "manifest_incomplete"
        assert "missing_field" in body
        assert "symbol" in body["missing_field"]

    async def test_400_when_strategy_class_unknown(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        """Everything staged but caller named an Engine-Lab class that
        doesn't exist."""
        run_id = "cross_bad_strat_class"
        ws = resolve_workspace(run_id, patched_artifacts_root)
        ws.ensure_layout()
        _write_valid_result_json(run_id=run_id, artifacts_root=patched_artifacts_root)
        _write_cross_run_manifest(run_id=run_id, artifacts_root=patched_artifacts_root)
        _stage_minute_data(run_id=run_id, artifacts_root=patched_artifacts_root)

        r = await client.post(
            f"/api/lean-sidecar/runs/{run_id}/cross-reconcile",
            json={"engine_lab_strategy_class": "DefinitelyNotAStrategy"},
        )
        assert r.status_code == 400
        body = r.json()["detail"]
        assert body["reason"] == "strategy_not_found"
        assert body["engine_lab_strategy_class"] == "DefinitelyNotAStrategy"

    async def test_assert_fees_true_promotes_commission_drift_to_gating(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        """Phase 5g.3 Branch-A: when assert_fees=true, COMMISSION_DRIFT
        flips from diagnostic to gating in the report. The signal is
        not the row count (drift may be 0 anyway in a synthetic test),
        but the echoed-back ``assert_fees: true`` and the documented
        gating set in counts_by_category vs gating_divergent_count.

        Here we just confirm the flag plumbs through end-to-end and
        the report shape is right."""
        run_id = "cross_assert_fees"
        ws = resolve_workspace(run_id, patched_artifacts_root)
        ws.ensure_layout()
        _write_valid_result_json(run_id=run_id, artifacts_root=patched_artifacts_root)
        _write_cross_run_manifest(run_id=run_id, artifacts_root=patched_artifacts_root)
        _stage_minute_data(run_id=run_id, artifacts_root=patched_artifacts_root)

        r = await client.post(
            f"/api/lean-sidecar/runs/{run_id}/cross-reconcile",
            json={
                "engine_lab_strategy_class": "BuyAndHoldStrategy",
                "assert_fees": True,
            },
        )
        assert r.status_code == 200, r.json()
        body = r.json()
        assert body["assert_fees"] is True

    async def test_assert_fees_true_requires_ibkr_brokerage(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        """Open-Q2 review-fix: ``assert_fees=true`` against a
        default-brokerage LEAN run is meaningless — promoting
        ``commission_drift`` to gating requires both sides to pin the
        same fee model. The endpoint refuses at the boundary with 400
        ``assert_fees_requires_ibkr_brokerage`` and surfaces the
        manifest's actual policy in ``detail`` so the operator can
        re-run with ``template='reconciliation'``."""
        run_id = "cross_assert_fees_wrong_policy"
        ws = resolve_workspace(run_id, patched_artifacts_root)
        ws.ensure_layout()
        _write_valid_result_json(run_id=run_id, artifacts_root=patched_artifacts_root)
        _write_cross_run_manifest(
            run_id=run_id,
            artifacts_root=patched_artifacts_root,
            brokerage_policy="lean_default",  # not IBKR
        )
        _stage_minute_data(run_id=run_id, artifacts_root=patched_artifacts_root)

        r = await client.post(
            f"/api/lean-sidecar/runs/{run_id}/cross-reconcile",
            json={
                "engine_lab_strategy_class": "BuyAndHoldStrategy",
                "assert_fees": True,
            },
        )
        assert r.status_code == 400
        body = r.json()["detail"]
        assert body["reason"] == "assert_fees_requires_ibkr_brokerage"
        assert body["manifest_brokerage_policy"] == "lean_default"

    async def test_assert_fees_false_does_not_require_ibkr_brokerage(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        """The guard only fires when ``assert_fees=true``. A default-
        brokerage LEAN run with ``assert_fees=false`` is the normal
        diagnostic-only path and must succeed."""
        run_id = "cross_no_assert_fees_default_brokerage"
        ws = resolve_workspace(run_id, patched_artifacts_root)
        ws.ensure_layout()
        _write_valid_result_json(run_id=run_id, artifacts_root=patched_artifacts_root)
        _write_cross_run_manifest(
            run_id=run_id,
            artifacts_root=patched_artifacts_root,
            brokerage_policy="lean_default",
        )
        _stage_minute_data(run_id=run_id, artifacts_root=patched_artifacts_root)

        r = await client.post(
            f"/api/lean-sidecar/runs/{run_id}/cross-reconcile",
            json={
                "engine_lab_strategy_class": "BuyAndHoldStrategy",
                "assert_fees": False,
            },
        )
        assert r.status_code == 200, r.json()

    async def test_404_when_workspace_missing(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        r = await client.post(
            "/api/lean-sidecar/runs/cross_no_workspace/cross-reconcile",
            json={"engine_lab_strategy_class": "BuyAndHoldStrategy"},
        )
        assert r.status_code == 404
        assert r.json()["detail"]["reason"] == "run_not_found"

    async def test_404_when_normalized_missing(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        """A workspace that exists but where LEAN crashed before producing
        parseable artifacts. The cross-reconciler 404s on the same
        ``normalized_missing`` reason the Phase 5a self-reconciler uses,
        so a frontend that handles one branch handles both."""
        ws = resolve_workspace("cross_no_artifacts", patched_artifacts_root)
        ws.ensure_layout()
        # Intentionally do NOT write normalized_dir/result.json.

        r = await client.post(
            "/api/lean-sidecar/runs/cross_no_artifacts/cross-reconcile",
            json={"engine_lab_strategy_class": "BuyAndHoldStrategy"},
        )
        assert r.status_code == 404
        assert r.json()["detail"]["reason"] == "normalized_missing"

    async def test_404_when_result_json_malformed(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        ws = resolve_workspace("cross_malformed", patched_artifacts_root)
        ws.ensure_layout()
        ws.normalized_dir.mkdir(parents=True, exist_ok=True)
        (ws.normalized_dir / "result.json").write_text(
            "{not valid json",
            encoding="utf-8",
        )

        r = await client.post(
            "/api/lean-sidecar/runs/cross_malformed/cross-reconcile",
            json={"engine_lab_strategy_class": "BuyAndHoldStrategy"},
        )
        assert r.status_code == 404
        assert r.json()["detail"]["reason"] == "normalized_missing"

    async def test_invalid_run_id_rejected_at_cross_reconcile(
        self,
        client: AsyncClient,
    ) -> None:
        r = await client.post(
            "/api/lean-sidecar/runs/..escape/cross-reconcile",
            json={"engine_lab_strategy_class": "BuyAndHoldStrategy"},
        )
        assert r.status_code == 400

    async def test_422_when_strategy_class_missing(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        """``engine_lab_strategy_class`` has no default — per D3, no
        auto-derivation; the caller MUST name the class explicitly."""
        ws = resolve_workspace("cross_no_class", patched_artifacts_root)
        ws.ensure_layout()
        r = await client.post(
            "/api/lean-sidecar/runs/cross_no_class/cross-reconcile",
            json={},
        )
        assert r.status_code == 422

    async def test_422_when_strategy_class_empty_string(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        """Empty-string class names are guarded — they'd silently no-op
        on the engine-lab side and produce a deceptively-clean report."""
        ws = resolve_workspace("cross_empty_class", patched_artifacts_root)
        ws.ensure_layout()
        r = await client.post(
            "/api/lean-sidecar/runs/cross_empty_class/cross-reconcile",
            json={"engine_lab_strategy_class": ""},
        )
        assert r.status_code == 422

    async def test_422_when_extra_fields_passed(
        self,
        client: AsyncClient,
        patched_artifacts_root: Path,
    ) -> None:
        """``extra='forbid'`` matches the rest of the lean_sidecar
        request models — a typo (``assert_fee`` vs ``assert_fees``)
        must 422, not silently default to False."""
        ws = resolve_workspace("cross_extra_field", patched_artifacts_root)
        ws.ensure_layout()
        r = await client.post(
            "/api/lean-sidecar/runs/cross_extra_field/cross-reconcile",
            json={
                "engine_lab_strategy_class": "BuyAndHoldStrategy",
                "assert_fee": True,  # typo
            },
        )
        assert r.status_code == 422

    async def test_response_model_exposed_in_openapi_schema(self) -> None:
        """Phase 5g.1 contract: even though the scaffold returns 501, the
        response model is registered with FastAPI so the OpenAPI schema
        documents it. Frontend Phase 5g.4 can codegen against it now."""
        from app.main import app

        schema = app.openapi()
        components = schema.get("components", {}).get("schemas", {})
        assert "CrossEngineReconciliationReportModel" in components
        assert "CrossReconcileRequestModel" in components

        report = components["CrossEngineReconciliationReportModel"]
        # schema_version default of 1 must be visible — the consumer
        # contract per D10 is "fail-fast on unrecognized version", so
        # the codegen needs to see what the current version IS.
        assert report["properties"]["schema_version"]["default"] == 1


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
