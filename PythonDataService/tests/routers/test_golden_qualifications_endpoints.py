"""Golden Search qualified versions over HTTP: status, the default, the Deploy offer, revoke and re-proof (#2696).

Live Postgres only (``POSTGRES_URL_IS_EPHEMERAL=1``); every test seeds a stock
of its own. Re-proof replays a real proof over a seeded, catalog-admitted lake
from a temporary blob store, as ``tests/research/golden_search/test_proof.py``
does.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from app.config import settings
from app.data_lake.catalog_client import CatalogUnavailableError
from app.data_lake.path_policy import lake_subpath
from app.lean_sidecar.trading_calendar import expected_sessions
from app.main import app
from app.research.golden_search import qualification_service
from app.research.golden_search.proof import BlobStore, ProofRecord, ProofWindow, build_proof
from app.research.golden_search.qualification_service import QUALIFICATION_UNJUDGEABLE
from app.research.golden_search.qualifications import get_default
from app.research.persistence.db import with_connection
from app.research.sweep.snapshot import capture_data_snapshot
from app.schemas.run_admission import QUALIFICATION_REVOKED, QUALIFICATION_STALE
from app.services import signal_program_admission as admission_module
from app.services.signal_program_admission import running_build_digests
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.golden_qualification import CONTRACT, PROGRAM, canonical_point, seed_qualification
from tests._helpers.lean_store import seed_store_day
from tests.services.test_signal_program_admission import _copied_source_tree

BASE = "/api/research/golden-qualifications"
WINDOW = ProofWindow(
    start_ms=et_midnight_ms(date(2025, 2, 4)),
    end_ms=et_midnight_ms(date(2025, 2, 7)),
    warmup_from_ms=et_midnight_ms(date(2025, 2, 3)),
)


@pytest.fixture(autouse=True)
def _requires_ephemeral_db() -> None:
    if not os.getenv("POSTGRES_URL") or os.getenv("POSTGRES_URL_IS_EPHEMERAL", "").lower() not in ("1", "true"):
        pytest.skip("live-DB endpoint tests need an ephemeral POSTGRES_URL")


@pytest.fixture
def symbol() -> str:
    return f"GQ{uuid.uuid4().hex[:8].upper()}"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


def _running() -> str:
    return running_build_digests(CONTRACT)[0]


async def _seed(symbol: str, **kwargs: object):
    params = kwargs.pop("params", canonical_point(symbol, rsi_min=45.0))
    return await with_connection(
        seed_qualification,
        qualification_id=kwargs.pop("qualification_id", f"gq-{uuid.uuid4().hex[:12]}"),
        symbol=symbol,
        params=params,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
async def test_ready_default_is_listed_offered_and_shown(client: httpx.AsyncClient, symbol: str) -> None:
    row = await _seed(
        symbol,
        artifact_digest=_running(),
        make_default=True,
        research={"exam_outcome": "not_enough_evidence", "claim": "exploratory", "research_override": True},
    )

    listed = await client.get(BASE, params={"symbol": symbol.lower()})
    defaults = await client.get(f"{BASE}/defaults", params={"program_key": PROGRAM, "symbol": symbol})
    detail = await client.get(f"{BASE}/{row.id}")
    offer = await client.get(f"{BASE}/{row.id}/deploy-offer")

    assert listed.status_code == 200, listed.text
    (summary,) = listed.json()
    assert summary["id"] == row.id
    assert summary["status"] == "ready"
    assert summary["is_default"] is True
    assert summary["parameters"] == {name: value for name, value in row.params.items() if name != "symbol"}
    assert summary["research"]["research_override"] is True
    assert summary["research"]["exam_outcome"] == "not_enough_evidence"
    assert defaults.status_code == 200
    assert [(item["qualification_id"], item["revision"], item["status"]) for item in defaults.json()] == [
        (row.id, 1, "ready")
    ]
    assert detail.status_code == 200
    assert detail.json()["params_sha256"] == row.params_sha256
    assert detail.json()["events"] == []
    assert offer.status_code == 200
    body = offer.json()
    assert body["qualification_id"] == row.id
    assert body["program_key"] == PROGRAM
    assert body["program_version"] == CONTRACT.program_version
    assert body["symbol"] == symbol
    assert "symbol" not in body["parameters"]
    assert body["parameters"]["rsi_min"] == 45.0
    assert body["status"] == "ready" and body["is_default"] is True
    assert row.study_id[:8] in body["explanation"]
    assert "2025-10-01" in body["explanation"]  # approval date, ET


async def test_a_version_proven_on_other_bytes_is_offered_as_stale(client: httpx.AsyncClient, symbol: str) -> None:
    row = await _seed(symbol, artifact_digest="0" * 64)

    offer = await client.get(f"{BASE}/{row.id}/deploy-offer")

    assert offer.status_code == 200
    assert offer.json()["status"] == "stale"
    assert offer.json()["explanation"] == QUALIFICATION_STALE
    assert offer.json()["is_default"] is False


async def test_an_unknown_qualification_is_404(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{BASE}/gq-missing/deploy-offer")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "QUALIFICATION_NOT_FOUND"


async def test_a_process_that_cannot_name_its_build_never_reads_ready(
    client: httpx.AsyncClient, symbol: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = await _seed(symbol, artifact_digest=_running(), make_default=True)
    monkeypatch.setattr(admission_module, "_SERVICE_ROOT", _copied_source_tree(tmp_path, drift_artifact=True))

    offer = await client.get(f"{BASE}/{row.id}/deploy-offer")

    assert offer.json()["status"] == "unverifiable"
    assert offer.json()["explanation"] == QUALIFICATION_UNJUDGEABLE


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------
async def test_revoking_the_default_clears_the_pointer_with_history(client: httpx.AsyncClient, symbol: str) -> None:
    row = await _seed(symbol, artifact_digest=_running(), make_default=True)
    body = {"reason": "The final test was reused.", "idempotencyKey": f"revoke-{symbol}"}

    revoked = await client.post(f"{BASE}/{row.id}/revoke", json=body)
    replayed = await client.post(f"{BASE}/{row.id}/revoke", json=body)

    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["status_explanation"] == QUALIFICATION_REVOKED
    assert revoked.json()["is_default"] is False
    assert [event["kind"] for event in revoked.json()["events"]] == ["revoked"]
    assert replayed.status_code == 200
    assert len(replayed.json()["events"]) == 1
    pointer = await with_connection(get_default, PROGRAM, symbol)
    assert pointer is not None and pointer.qualification_id is None and pointer.revision == 2
    history = await with_connection(
        lambda conn: conn.fetch(
            """
            SELECT previous_qualification_id, qualification_id, revision FROM research_golden_default_history
             WHERE program_key = $1 AND symbol = $2 ORDER BY revision
            """,
            PROGRAM,
            symbol,
        )
    )
    assert [tuple(item) for item in history] == [(None, row.id, 1), (row.id, None, 2)]
    defaults = await client.get(f"{BASE}/defaults", params={"symbol": symbol})
    assert defaults.json() == []


async def test_revoking_a_version_that_is_not_the_default_leaves_the_pointer(
    client: httpx.AsyncClient, symbol: str
) -> None:
    default = await _seed(symbol, artifact_digest=_running(), make_default=True)
    other = await _seed(symbol, artifact_digest=_running(), params=canonical_point(symbol, rsi_min=44.0))

    response = await client.post(
        f"{BASE}/{other.id}/revoke", json={"reason": "Superseded.", "idempotency_key": f"revoke-{symbol}"}
    )

    assert response.status_code == 200
    pointer = await with_connection(get_default, PROGRAM, symbol)
    assert pointer is not None and pointer.qualification_id == default.id and pointer.revision == 1


async def test_revoke_refusals(client: httpx.AsyncClient, symbol: str) -> None:
    row = await _seed(symbol, artifact_digest=_running())
    first = {"reason": "The final test was reused.", "idempotency_key": f"revoke-{symbol}"}
    await client.post(f"{BASE}/{row.id}/revoke", json=first)

    conflict = await client.post(f"{BASE}/{row.id}/revoke", json={**first, "reason": "Another reason."})
    again = await client.post(
        f"{BASE}/{row.id}/revoke", json={"reason": "Another reason.", "idempotency_key": f"other-{symbol}"}
    )
    missing = await client.post(f"{BASE}/gq-missing/revoke", json=first)
    blank = await client.post(f"{BASE}/{row.id}/revoke", json={"reason": "   ", "idempotency_key": f"blank-{symbol}"})
    bad_key = await client.post(f"{BASE}/{row.id}/revoke", json={"reason": "Fine reason.", "idempotency_key": "x"})

    assert (conflict.status_code, conflict.json()["detail"]["code"]) == (409, "IDEMPOTENCY_CONFLICT")
    assert (again.status_code, again.json()["detail"]["code"]) == (409, "QUALIFICATION_REVOKED")
    assert (missing.status_code, missing.json()["detail"]["code"]) == (404, "QUALIFICATION_NOT_FOUND")
    assert blank.status_code == 422
    assert bad_key.status_code == 422


# ---------------------------------------------------------------------------
# Re-proof
# ---------------------------------------------------------------------------
@pytest.fixture
def proven(
    tmp_path: Path, symbol: str, seeded_lake_catalog, monkeypatch: pytest.MonkeyPatch
) -> tuple[ProofRecord, BlobStore]:
    """A real proof of the candidate over a seeded lake, its blobs in the store the endpoint uses."""
    lake = tmp_path / "writer-root" / lake_subpath("polygon_split_adjusted")
    lake.mkdir(parents=True)
    for day in expected_sessions(date(2025, 2, 3), date(2025, 2, 14)):
        seed_store_day(lake, symbol, day)
    snapshot = capture_data_snapshot(
        roots=[lake], symbol=symbol, resolution="minute", data_start=date(2025, 2, 3), data_end=date(2025, 2, 14)
    )
    blobs = BlobStore(tmp_path / "blobs")
    monkeypatch.setattr(settings, "GOLDEN_SEARCH_PROOF_ROOT", blobs.root)
    artifact_digest, wiring_digest = running_build_digests(CONTRACT)
    record = build_proof(
        strategy_key=PROGRAM,
        symbol=symbol,
        params=canonical_point(symbol, rsi_min=45.0),
        window=WINDOW,
        snapshot=snapshot,
        roots=[lake],
        blob_store=blobs,
        artifact_digest=artifact_digest,
        wiring_digest=wiring_digest,
        created_at_ms=1_759_300_000_000,
    )
    return record, blobs


async def test_reprove_appends_fresh_evidence_once(
    client: httpx.AsyncClient, symbol: str, proven: tuple[ProofRecord, BlobStore]
) -> None:
    record, _blobs = proven
    row = await _seed(symbol, artifact_digest=record.artifact_digest, proof=record.as_dict(), params=record.params)
    body = {"idempotency_key": f"reprove-{symbol}"}

    reproved = await client.post(f"{BASE}/{row.id}/reprove", json=body)
    replayed = await client.post(f"{BASE}/{row.id}/reprove", json=body)

    assert reproved.status_code == 200, reproved.text
    (event,) = reproved.json()["events"]
    assert event["kind"] == "reproved"
    assert (event["artifact_digest"], event["wiring_digest"]) == running_build_digests(CONTRACT)
    assert reproved.json()["status"] == "ready"
    assert replayed.status_code == 200
    assert len(replayed.json()["events"]) == 1


async def test_reprove_refuses_a_corrupted_input(
    client: httpx.AsyncClient, symbol: str, proven: tuple[ProofRecord, BlobStore]
) -> None:
    record, blobs = proven
    row = await _seed(symbol, artifact_digest=record.artifact_digest, proof=record.as_dict(), params=record.params)
    digest = next(iter(record.manifest.values()))
    (blobs.root / digest[:2] / digest).write_bytes(b"torn")

    response = await client.post(f"{BASE}/{row.id}/reprove", json={"idempotency_key": f"reprove-{symbol}"})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PROOF_MISMATCH"
    assert "BLOB_CORRUPT" in response.json()["detail"]["message"]
    detail = await client.get(f"{BASE}/{row.id}")
    assert detail.json()["events"] == []


async def test_reprove_refuses_a_stored_proof_that_no_longer_hashes_to_its_digest(
    client: httpx.AsyncClient, symbol: str, proven: tuple[ProofRecord, BlobStore]
) -> None:
    record, _blobs = proven
    # A field the record does not carry: the stored JSON hashes differently from the record it reads back as.
    tampered = {**record.as_dict(), "unrecorded": True}
    row = await _seed(symbol, artifact_digest=record.artifact_digest, proof=tampered, params=record.params)

    response = await client.post(f"{BASE}/{row.id}/reprove", json={"idempotency_key": f"reprove-{symbol}"})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PROOF_MISMATCH"


async def test_reprove_never_revives_a_revoked_version(
    client: httpx.AsyncClient, symbol: str, proven: tuple[ProofRecord, BlobStore]
) -> None:
    record, _blobs = proven
    row = await _seed(symbol, artifact_digest=record.artifact_digest, proof=record.as_dict(), params=record.params)
    await client.post(f"{BASE}/{row.id}/revoke", json={"reason": "Withdrawn.", "idempotency_key": f"revoke-{symbol}"})

    response = await client.post(f"{BASE}/{row.id}/reprove", json={"idempotency_key": f"reprove-{symbol}"})

    assert (response.status_code, response.json()["detail"]["code"]) == (409, "QUALIFICATION_REVOKED")


async def test_reprove_refuses_while_a_restart_is_needed(
    client: httpx.AsyncClient,
    symbol: str,
    proven: tuple[ProofRecord, BlobStore],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    record, _blobs = proven
    row = await _seed(symbol, artifact_digest=record.artifact_digest, proof=record.as_dict(), params=record.params)
    monkeypatch.setattr(admission_module, "_SERVICE_ROOT", _copied_source_tree(tmp_path / "src", drift_artifact=True))

    response = await client.post(f"{BASE}/{row.id}/reprove", json={"idempotency_key": f"reprove-{symbol}"})

    assert (response.status_code, response.json()["detail"]["code"]) == (409, "RESTART_NEEDED")


async def test_reprove_answers_503_while_the_catalog_is_down(
    client: httpx.AsyncClient, symbol: str, proven: tuple[ProofRecord, BlobStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    record, _blobs = proven
    row = await _seed(symbol, artifact_digest=record.artifact_digest, proof=record.as_dict(), params=record.params)

    def _catalog_down(*_args: object, **_kwargs: object) -> ProofRecord:
        raise CatalogUnavailableError("could not reach the catalog Postgres")

    monkeypatch.setattr(qualification_service, "reprove", _catalog_down)

    response = await client.post(f"{BASE}/{row.id}/reprove", json={"idempotency_key": f"reprove-{symbol}"})

    assert (response.status_code, response.json()["detail"]["code"]) == (503, "CATALOG_UNAVAILABLE")
