"""Phase 5f — determinism gate (E2E, requires LEAN image).

The ADR's invariant #15 says reconciliation fixtures require a
determinism proof: running the trusted sample twice with identical
inputs must produce equivalent artifacts. Phase 1c added the
clean-run contract; Phase 5d/5e populated the window + bar-consumption
manifest fields; this test gates that the artifacts those produce are
actually byte-stable across re-runs.

What's required equal per mission-critical doc D2:
- Every hash in ``staged_data.bar_zips`` (the inputs LEAN saw)
- The normalized ``equity_curve`` serialized byte stream
- The normalized ``order_events`` list serialized
- ``algorithm_source_sha256``, ``config_json_sha256``
- ``parameters`` (start/end date, starting_cash, symbol)
- ``data_adjustment_policy``, ``data_normalization_mode``, ``fill_forward``,
  ``brokerage_policy``, ``starting_capital``, ``account_currency``
- ``requested_window_ms``, ``staged_data_window_ms``, ``bars_consumed_by_symbol``
- ``lean_image_digest`` (obvious — same image)

What's allowed to differ (timing-derived):
- ``started_at_ms``, ``finished_at_ms`` (wall-clock)
- ``duration_ms`` in the response
- ``run_id`` (we explicitly use different ones to test alongside-store)
- ``effective_algorithm_window_ms`` MAY shift by quantization if LEAN's
  ResultsAnalyzer rounds tickwise — accepted within the
  ``DETERMINISM_TIMING_FIELDS`` allow-list below; surface anything else
  that differs as a real determinism bug.

Gated on ``requires_lean_image`` — CI skips it; humans run it after
``podman pull docker.io/quantconnect/lean:<digest>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import httpx
import pytest
import uvicorn
from httpx import ASGITransport, AsyncClient

from app.lean_sidecar import config as sidecar_config
from app.lean_sidecar.config import PINNED_LEAN_IMAGE_DIGEST
from app.lean_sidecar.launcher.app import app as launcher_app
from app.main import app as data_plane_app

pytestmark = [
    pytest.mark.requires_lean_image,
    pytest.mark.slow,
    pytest.mark.asyncio,
]


# Manifest fields the determinism gate ignores. Anything NOT in this set
# must match across both runs. Each field below has a justification:
#
# - run_id: we explicitly use different ones (det_a / det_b).
# - started_at_ms / finished_at_ms: wall-clock of the run.
# - notes: includes derived strings that interpolate timing-flavored
#   values (e.g., the run could in principle pick up a different
#   normalized_parser stamp string if the parser version had changed —
#   but for two runs in the same test session they're identical except
#   for stylistic ordering risks; we exclude defensively).
_ALLOWED_TO_DIFFER_MANIFEST_FIELDS: frozenset[str] = frozenset(
    {"run_id", "started_at_ms", "finished_at_ms", "notes"}
)

# Normalized-result fields excluded from the byte-equality check.
# ``algorithm_id`` is allowed to differ if LEAN ever derives it from
# the run_id (we don't think it does, but defensive). Otherwise the
# normalized result must be byte-identical between runs.
_ALLOWED_TO_DIFFER_NORMALIZED_FIELDS: frozenset[str] = frozenset(set())


@pytest.fixture
def patched_artifacts_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = (tmp_path / "artifacts").resolve()
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sidecar_config, "DEFAULT_ARTIFACTS_ROOT", root)
    from app.routers import lean_sidecar as lean_sidecar_router
    from app.services import lean_sidecar_service

    monkeypatch.setattr(lean_sidecar_service, "DEFAULT_ARTIFACTS_ROOT", root)
    monkeypatch.setattr(lean_sidecar_router, "DEFAULT_ARTIFACTS_ROOT", root)
    monkeypatch.setenv("LEAN_LAUNCHER_ARTIFACTS_ROOT", str(root))
    return root


@contextlib.asynccontextmanager
async def _running_launcher(port: int):
    config = uvicorn.Config(
        launcher_app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        task.cancel()
        raise RuntimeError("launcher did not start within 2.5s")
    try:
        yield
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)


def _pick_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _strip_keys(obj: dict, keys: frozenset[str]) -> dict:
    """Return a shallow copy of obj with the keys removed."""
    return {k: v for k, v in obj.items() if k not in keys}


async def _post_trusted_run(client: AsyncClient, run_id: str) -> dict:
    response = await client.post(
        "/api/lean-sidecar/trusted-runs",
        json={
            "run_id": run_id,
            "symbol": "SPY",
            # 2025-01-06 .. 2025-01-10 (Mon-Fri), represented by the
            # session-open ms of the first session and next trading day.
            "start_ms_utc": 1_736_173_800_000,
            "end_ms_utc": 1_736_778_600_000,
            "starting_cash": 100000.0,
        },
        timeout=httpx.Timeout(300.0),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["exit_code"] == 0, body
    assert not body["timed_out"]
    return body


class TestDeterminismGate:
    """Per ADR invariant #15: two same-input runs must produce equivalent
    artifacts. Anything that legitimately needs to differ (wall-clock,
    run_id) is enumerated in the allow-lists at module top; everything
    else must match byte-for-byte."""

    async def test_two_runs_same_inputs_produce_equivalent_artifacts(
        self,
        patched_artifacts_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        if PINNED_LEAN_IMAGE_DIGEST is None:
            pytest.skip("PINNED_LEAN_IMAGE_DIGEST not set")

        port = _pick_free_port()
        monkeypatch.setenv("LEAN_LAUNCHER_URL", f"http://127.0.0.1:{port}")

        async with _running_launcher(port):
            transport = ASGITransport(app=data_plane_app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                await _post_trusted_run(client, "det_a")
                await _post_trusted_run(client, "det_b")

                # Fetch each run's manifest + normalized result via the
                # public inspection endpoints — the same path an operator
                # or auditor would use.
                manifest_a = (await client.get("/api/lean-sidecar/runs/det_a/manifest")).json()
                manifest_b = (await client.get("/api/lean-sidecar/runs/det_b/manifest")).json()
                normalized_a = (await client.get("/api/lean-sidecar/runs/det_a/normalized")).json()
                normalized_b = (await client.get("/api/lean-sidecar/runs/det_b/normalized")).json()

        # Manifest equality outside the allowed-to-differ fields. The
        # comparison is on dict-of-dicts so a top-level diff surfaces
        # which subtree changed.
        manifest_a_stable = _strip_keys(manifest_a, _ALLOWED_TO_DIFFER_MANIFEST_FIELDS)
        manifest_b_stable = _strip_keys(manifest_b, _ALLOWED_TO_DIFFER_MANIFEST_FIELDS)
        assert manifest_a_stable == manifest_b_stable, (
            "Manifest differs across two same-input runs:\n"
            f"A only: {set(manifest_a_stable) - set(manifest_b_stable)}\n"
            f"B only: {set(manifest_b_stable) - set(manifest_a_stable)}\n"
            f"diff: {_dict_diff(manifest_a_stable, manifest_b_stable)}"
        )

        # Normalized result: equity_curve + order_events + statistics
        # must be byte-identical. The serializer is the same so dict
        # equality is fine; for a tighter check we hash json.dumps with
        # sort_keys=True.
        normalized_a_stable = _strip_keys(normalized_a, _ALLOWED_TO_DIFFER_NORMALIZED_FIELDS)
        normalized_b_stable = _strip_keys(normalized_b, _ALLOWED_TO_DIFFER_NORMALIZED_FIELDS)
        a_serialized = json.dumps(normalized_a_stable, sort_keys=True)
        b_serialized = json.dumps(normalized_b_stable, sort_keys=True)
        assert a_serialized == b_serialized, (
            "Normalized result differs across two same-input runs.\n"
            f"diff (first 1k chars): {_string_diff(a_serialized, b_serialized)[:1024]}"
        )

        # Belt-and-suspenders on the load-bearing equality classes the
        # ADR specifically calls out, with friendly per-field messages
        # so a future regression points at the broken invariant.
        assert manifest_a["staged_data"]["bar_zips"] == manifest_b["staged_data"]["bar_zips"], (
            "staged_data.bar_zips hashes drift between runs — staging is not deterministic"
        )
        assert manifest_a["algorithm_source_sha256"] == manifest_b["algorithm_source_sha256"], (
            "algorithm_source_sha256 drifts — source staging is not deterministic"
        )
        assert manifest_a["config_json_sha256"] == manifest_b["config_json_sha256"], (
            "config_json_sha256 drifts — LeanConfig serialization is not deterministic"
        )
        assert manifest_a["bars_consumed_by_symbol"] == manifest_b["bars_consumed_by_symbol"], (
            "bars_consumed_by_symbol drifts — observations.csv shape is not deterministic"
        )


def _dict_diff(a: dict, b: dict) -> dict[str, tuple]:
    """Return {key: (a_value, b_value)} for keys whose values differ.
    Shallow — for nested mismatches you'll see the nested dict whole."""
    keys = set(a) | set(b)
    out: dict[str, tuple] = {}
    for k in keys:
        if a.get(k) != b.get(k):
            out[k] = (a.get(k), b.get(k))
    return out


def _string_diff(a: str, b: str) -> str:
    """Return the first ~200 chars on either side of the first divergence
    so the assertion message points at the relevant chunk."""
    for i, (ca, cb) in enumerate(zip(a, b, strict=False)):
        if ca != cb:
            start = max(0, i - 80)
            end = min(max(len(a), len(b)), i + 120)
            return f"at offset {i}:\n  A: ...{a[start:end]!r}...\n  B: ...{b[start:end]!r}..."
    if len(a) != len(b):
        return f"length differs: A={len(a)}, B={len(b)}"
    return "(no divergence found — equal strings?)"
