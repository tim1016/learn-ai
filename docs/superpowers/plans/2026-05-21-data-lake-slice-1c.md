# Data Lake Slice 1c Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Slice 1b `fake_polygon` stubs for the remaining artifact kinds — LEAN metadata (Phase 0 bootstrap), factor files (Polygon splits + dividends), map files (Polygon ticker events), derived daily-trade (aggregated from minute-trade), derived quote (synthesized from same-day minute-trade). Lock `data_contract_hash` as a deterministic hash instead of the `'x' * 64` placeholder.

**Architecture:** Phase 0 metadata extraction lands first so `sessions.py` can upgrade from its hardcoded 2024–2026 holiday list to a real parser of LEAN's `market-hours-database.json`. Factor and map files each get their own polygon-fetch + LEAN-CSV-writer pair. Derived artifacts (daily aggregation + quote synthesis) materialize *within* the same `ensure_data` invocation, after their source minute-trade artifacts complete. `data_contract_hash` becomes `sha256` over canonical `{provider, provider_params, price_adjustment_mode, session_policy, lean_format_version}` — proves "same contract" at the catalog level for forward extensibility.

**Tech Stack:** Python 3.12 + asyncpg + httpx + respx; Postgres 16; FastAPI; the existing podman-based LEAN-image metadata extraction lifted (cleanly, no cross-module import) from `app/lean_sidecar/staging.py`.

**Spec:** [`docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md`](../specs/2026-05-20-polygon-lean-data-lake-design.md) — §§ 4.5 (session-aware expansion + Phase 0 bootstrap), 4.6 (batched fetch, derived artifacts), 5.1 (file layout for factor/map files + metadata).

**Prior slice:** Slice 1b landed at master `37b3603d` — atomic write, lean_writer, polygon_fetcher, catalog write ops, sweep primitive, real-Polygon `ensure_data` dispatch for minute-trade. Non-minute-trade kinds still on `fake_polygon` stub.

---

## File structure

### New files

| File | Responsibility |
|---|---|
| `PythonDataService/app/data_lake/lean_metadata.py` | Extract `market-hours-database.json` + `symbol-properties-database.csv` from the pinned LEAN image via the existing launcher's `/extract-metadata` endpoint (and fall back to local `podman cp` when available). Returns `(market_hours_bytes, symbol_properties_bytes)`. |
| `PythonDataService/app/data_lake/polygon_corp_actions.py` | `fetch_splits(symbol, api_key)`, `fetch_dividends(symbol, api_key)` paginated GET against `/v3/reference/splits` and `/v3/reference/dividends`. Returns sorted normalized event lists. |
| `PythonDataService/app/data_lake/factor_files.py` | Convert sorted splits + dividends into LEAN's `factor_files/<sym>.csv` format. Builds the cumulative back-adjustment factors. `build_factor_file_bytes(symbol, splits, dividends, anchor_date, anchor_close_price) → bytes`. |
| `PythonDataService/app/data_lake/polygon_ticker_events.py` | `fetch_ticker_events(symbol, api_key)` against `/v3/reference/tickers/{ticker}/events`. Returns sorted ticker-change events. |
| `PythonDataService/app/data_lake/map_files.py` | Convert ticker events into LEAN's `map_files/<sym>.csv` format. `build_map_file_bytes(symbol, events, history_start, history_end) → bytes`. |
| `PythonDataService/app/data_lake/derived_daily.py` | Aggregate per-symbol minute-trade artifacts into LEAN's `daily/<sym>.zip` format. `build_daily_zip_bytes(symbol, complete_minute_artifacts) → bytes`. |
| `PythonDataService/app/data_lake/derived_quote.py` | Per-day quote synthesis from same-day trade artifact. `build_minute_quote_zip_bytes(symbol, trading_date, trade_bars) → bytes`. |
| `PythonDataService/app/data_lake/data_contract.py` | Deterministic `data_contract_hash(provider, provider_params, price_adjustment_mode, session_policy, lean_format_version) → str` over canonical JSON. |
| `PythonDataService/tests/unit/data_lake/test_lean_metadata.py` | Mock the launcher + the local-podman-cp fallback; verify bytes are returned and idempotent. |
| `PythonDataService/tests/unit/data_lake/test_polygon_corp_actions.py` | respx-mocked tests for splits + dividends pagination + error mapping. |
| `PythonDataService/tests/unit/data_lake/test_factor_files.py` | Verify the LEAN factor-file CSV format on known input cases (no events, one split, multiple splits + dividends). |
| `PythonDataService/tests/unit/data_lake/test_polygon_ticker_events.py` | respx-mocked tests for ticker events. |
| `PythonDataService/tests/unit/data_lake/test_map_files.py` | Verify the LEAN map-file CSV format (no-change symbol, single ticker change). |
| `PythonDataService/tests/unit/data_lake/test_derived_daily.py` | Determinism + correct OHLCV aggregation across minute-trade input. |
| `PythonDataService/tests/unit/data_lake/test_derived_quote.py` | Quote synthesis from trade bars; deterministic output. |
| `PythonDataService/tests/unit/data_lake/test_data_contract.py` | Determinism + identical-contract-collision tests. |
| `PythonDataService/tests/integration/data_lake/test_ensure_data_all_kinds.py` | End-to-end: respx-mocked Polygon (aggs + splits + dividends + ticker-events) + real Postgres + tmp filesystem + mocked LEAN-launcher metadata extraction. Verifies catalog rows + on-disk files for ALL artifact kinds for SPY over a one-week window. |

### Modified files

| File | Change |
|---|---|
| `PythonDataService/app/data_lake/sessions.py` | Replace the hardcoded `_USA_FULL_HOLIDAYS` set with a parser of the staged `market-hours-database.json`. Keep a fallback hardcoded list for the bootstrap case (when metadata hasn't been staged yet — first-ever ensure_data call against a virgin lake). |
| `PythonDataService/app/data_lake/catalog_client.py` | Add `claim_corp_action_artifact`, `claim_metadata_artifact`, `claim_aggregated_bar_artifact` (per-kind partial-index ON CONFLICT INSERTs). |
| `PythonDataService/app/data_lake/ensure_data.py` | Add Phase 0 metadata staging before `expand_required_artifacts`. Replace the `fake_polygon` fall-through for non-minute-trade with real `_process_<kind>_artifact` helpers. Compute real `data_contract_hash` via `data_contract.data_contract_hash(...)`. |
| `PythonDataService/app/data_lake/fake_polygon.py` | Narrow further — `synth_artifact_record` now refuses every artifact kind that has a real implementation (effectively becomes a defensive boundary that only ever raises in Slice 1c). |
| `PythonDataService/app/data_lake/types.py` | Add `'corp_action_revision_mismatch'` to `ArtifactFailure.reason` Literal. |

---

## Tasks

### Task 1: `lean_metadata.py` — extract LEAN image metadata

**Files:**
- Create: `PythonDataService/app/data_lake/lean_metadata.py`
- Create: `PythonDataService/tests/unit/data_lake/test_lean_metadata.py`

- [ ] **Step 1: Write the failing tests**

```python
# PythonDataService/tests/unit/data_lake/test_lean_metadata.py
"""Unit tests for app.data_lake.lean_metadata.

The module delegates to the LEAN-sidecar launcher's POST /extract-metadata
endpoint (the launcher owns podman access; the data-plane container does
not have podman on PATH). For unit tests we mock the httpx call and assert
the returned bytes are surfaced unchanged.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.5
"""

from __future__ import annotations

import base64
import re

import httpx
import pytest
import respx

from app.data_lake.lean_metadata import (
    LeanMetadataExtractionError,
    extract_lean_metadata,
)


@pytest.mark.asyncio
@respx.mock
async def test_extracts_market_hours_and_symbol_properties():
    mh_bytes = b'{"exchange": "NYSE", "rule": "..."}'
    sp_bytes = b"SPY,equity,usd,1,0\n"
    respx.post(re.compile(r"http://[^/]+/extract-metadata")).mock(
        return_value=httpx.Response(
            200,
            json={
                "market_hours_database_b64": base64.b64encode(mh_bytes).decode("ascii"),
                "symbol_properties_database_b64": base64.b64encode(sp_bytes).decode("ascii"),
                "image_digest_used": "sha256:97884667...",
            },
        )
    )
    market_hours, symbol_properties = await extract_lean_metadata(
        image_digest="sha256:97884667...",
        launcher_url="http://launcher:8090",
        launcher_token="t",
    )
    assert market_hours == mh_bytes
    assert symbol_properties == sp_bytes


@pytest.mark.asyncio
@respx.mock
async def test_launcher_500_raises_extraction_error():
    respx.post(re.compile(r"http://[^/]+/extract-metadata")).mock(
        return_value=httpx.Response(500, text="boom")
    )
    with pytest.raises(LeanMetadataExtractionError):
        await extract_lean_metadata(
            image_digest="sha256:97884667...",
            launcher_url="http://launcher:8090",
            launcher_token="t",
        )


@pytest.mark.asyncio
@respx.mock
async def test_image_digest_mismatch_raises():
    """Defensive: if the launcher returns a different image digest than we asked
    for (e.g. it pulled latest), refuse the result."""
    respx.post(re.compile(r"http://[^/]+/extract-metadata")).mock(
        return_value=httpx.Response(
            200,
            json={
                "market_hours_database_b64": base64.b64encode(b"x").decode("ascii"),
                "symbol_properties_database_b64": base64.b64encode(b"y").decode("ascii"),
                "image_digest_used": "sha256:deadbeef",
            },
        )
    )
    with pytest.raises(LeanMetadataExtractionError):
        await extract_lean_metadata(
            image_digest="sha256:97884667...",
            launcher_url="http://launcher:8090",
            launcher_token="t",
        )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_lean_metadata.py -v
```
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `lean_metadata.py`**

```python
# PythonDataService/app/data_lake/lean_metadata.py
"""LEAN-image metadata extraction (data-lake-side counterpart).

The polygon-data-service container does not have `podman` on PATH, so it
cannot subprocess-spawn `podman cp` against the LEAN image directly. The
LEAN-sidecar launcher (a host process that DOES have podman) exposes
POST /extract-metadata; this module is the data-lake-side caller.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.5
Existing reference implementation:
  app/lean_sidecar/launcher_client.py — original caller for the lean-sidecar flow
  app/lean_sidecar/launcher/service.py::extract_metadata — launcher endpoint impl

NB: this is intentional duplication of the call path. app/lean_sidecar/ is
retired in Slice 1d; this module is the surviving canonical caller.
"""

from __future__ import annotations

import base64
import logging

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 60.0


class LeanMetadataExtractionError(RuntimeError):
    """Raised when the launcher can't / won't produce the metadata bytes."""


async def extract_lean_metadata(
    image_digest: str,
    launcher_url: str,
    launcher_token: str,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> tuple[bytes, bytes]:
    """Fetch (market_hours_database_bytes, symbol_properties_database_bytes).

    The launcher does the subprocess work; we just transport the bytes. The
    response is base64-encoded JSON to keep the launcher contract a simple
    POST/JSON pair (no multipart, no binary boundary parsing).

    Raises LeanMetadataExtractionError on any failure or digest mismatch.
    """
    url = launcher_url.rstrip("/") + "/extract-metadata"
    headers = {"X-Launcher-Token": launcher_token} if launcher_token else {}
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        try:
            resp = await client.post(
                url,
                json={"image_digest": image_digest},
                headers=headers,
            )
        except httpx.RequestError as e:
            raise LeanMetadataExtractionError(
                f"launcher unreachable at {url}: {e}"
            ) from e

    if resp.status_code != 200:
        raise LeanMetadataExtractionError(
            f"launcher /extract-metadata returned {resp.status_code}: {resp.text[:200]}"
        )

    payload = resp.json()
    used = payload.get("image_digest_used")
    if used and used != image_digest:
        raise LeanMetadataExtractionError(
            f"launcher used image_digest={used!r} but {image_digest!r} was requested"
        )

    try:
        mh = base64.b64decode(payload["market_hours_database_b64"])
        sp = base64.b64decode(payload["symbol_properties_database_b64"])
    except (KeyError, ValueError) as e:
        raise LeanMetadataExtractionError(
            f"launcher returned malformed payload: {e}"
        ) from e
    logger.info(
        "data_lake.lean_metadata: extracted %d bytes market-hours + %d bytes symbol-properties for %s",
        len(mh), len(sp), image_digest,
    )
    return mh, sp
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_lean_metadata.py -v
```
Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/data_lake/lean_metadata.py PythonDataService/tests/unit/data_lake/test_lean_metadata.py
git commit -m "feat(data-lake): LEAN-image metadata extraction caller (Slice 1c)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `data_contract.py` — deterministic hash

**Files:**
- Create: `PythonDataService/app/data_lake/data_contract.py`
- Create: `PythonDataService/tests/unit/data_lake/test_data_contract.py`

- [ ] **Step 1: Write the failing tests**

```python
# PythonDataService/tests/unit/data_lake/test_data_contract.py
"""Determinism + collision tests for app.data_lake.data_contract."""

from __future__ import annotations

from app.data_lake.data_contract import data_contract_hash


def test_identical_inputs_produce_identical_hashes():
    a = data_contract_hash(
        provider="polygon",
        provider_params={"adjusted": False, "timespan": "minute", "multiplier": 1},
        price_adjustment_mode="raw",
        session_policy="full",
        lean_format_version=1,
    )
    b = data_contract_hash(
        provider="polygon",
        provider_params={"multiplier": 1, "timespan": "minute", "adjusted": False},
        price_adjustment_mode="raw",
        session_policy="full",
        lean_format_version=1,
    )
    assert a == b  # key order in provider_params must not matter
    assert len(a) == 64


def test_different_provider_produces_different_hashes():
    a = data_contract_hash(
        provider="polygon", provider_params={}, price_adjustment_mode="raw",
        session_policy="full", lean_format_version=1,
    )
    b = data_contract_hash(
        provider="learn_ai_derived", provider_params={}, price_adjustment_mode="raw",
        session_policy="full", lean_format_version=1,
    )
    assert a != b


def test_different_provider_params_produce_different_hashes():
    a = data_contract_hash(
        provider="polygon", provider_params={"adjusted": False},
        price_adjustment_mode="raw", session_policy="full", lean_format_version=1,
    )
    b = data_contract_hash(
        provider="polygon", provider_params={"adjusted": True},
        price_adjustment_mode="raw", session_policy="full", lean_format_version=1,
    )
    assert a != b


def test_nested_provider_params_canonicalized():
    a = data_contract_hash(
        provider="learn_ai_derived",
        provider_params={"source": {"trade_artifact_id": 42, "sha256": "abc"}},
        price_adjustment_mode="raw", session_policy="full", lean_format_version=1,
    )
    b = data_contract_hash(
        provider="learn_ai_derived",
        provider_params={"source": {"sha256": "abc", "trade_artifact_id": 42}},
        price_adjustment_mode="raw", session_policy="full", lean_format_version=1,
    )
    assert a == b  # nested key order must not matter
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_data_contract.py -v
```
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `data_contract.py`**

```python
# PythonDataService/app/data_lake/data_contract.py
"""Deterministic data-contract fingerprint.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 3.1
("data_contract_hash" — proves same-contract identity at the catalog level).

`data_contract_hash` is sha256 over canonical JSON of:
  {provider, provider_params, price_adjustment_mode, session_policy,
   lean_format_version}

The hash is stable across nested key ordering thanks to `sort_keys=True`.
Two artifacts with the same hash are interchangeable consumers of the same
contract; the unique constraint enforces (market, symbol, ...) uniqueness on
top of that.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def data_contract_hash(
    provider: str,
    provider_params: dict[str, Any],
    price_adjustment_mode: str | None,
    session_policy: str,
    lean_format_version: int,
) -> str:
    """Compute the 64-char hex sha256 of the canonical-JSON fingerprint."""
    payload = {
        "provider": provider,
        "provider_params": provider_params,
        "price_adjustment_mode": price_adjustment_mode,
        "session_policy": session_policy,
        "lean_format_version": lean_format_version,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
```

- [ ] **Step 4: Run tests to verify they pass**

Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/data_lake/data_contract.py PythonDataService/tests/unit/data_lake/test_data_contract.py
git commit -m "feat(data-lake): deterministic data_contract_hash (Slice 1c)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `polygon_corp_actions.py` — splits + dividends

**Files:**
- Create: `PythonDataService/app/data_lake/polygon_corp_actions.py`
- Create: `PythonDataService/tests/unit/data_lake/test_polygon_corp_actions.py`

- [ ] **Step 1: Write the failing tests**

```python
# PythonDataService/tests/unit/data_lake/test_polygon_corp_actions.py
"""respx-mocked tests for Polygon corp-action endpoints."""

from __future__ import annotations

import re

import httpx
import pytest
import respx

from app.data_lake.polygon_corp_actions import (
    DividendEvent,
    SplitEvent,
    fetch_dividends,
    fetch_splits,
)

pytestmark = pytest.mark.asyncio


@respx.mock
async def test_fetch_splits_single_page():
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/splits.*")).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "OK",
                "results": [
                    {"ticker": "SPY", "execution_date": "2020-08-31",
                     "split_from": 1, "split_to": 4},
                ],
            },
        )
    )
    events = await fetch_splits(symbol="SPY", api_key="test-key")
    assert len(events) == 1
    assert events[0] == SplitEvent(
        execution_date="2020-08-31", split_from=1.0, split_to=4.0,
    )


@respx.mock
async def test_fetch_splits_pagination():
    page_a = respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/splits\?.*")).mock(
        return_value=httpx.Response(200, json={
            "status": "OK",
            "results": [
                {"ticker": "SPY", "execution_date": "2020-08-31",
                 "split_from": 1, "split_to": 4},
            ],
            "next_url": "https://api.polygon.io/v3/reference/splits/page2",
        })
    )
    page_b = respx.get("https://api.polygon.io/v3/reference/splits/page2").mock(
        return_value=httpx.Response(200, json={
            "status": "OK",
            "results": [
                {"ticker": "SPY", "execution_date": "2000-01-03",
                 "split_from": 1, "split_to": 2},
            ],
        })
    )
    events = await fetch_splits(symbol="SPY", api_key="test-key")
    assert len(events) == 2
    # Results are sorted ascending by execution_date.
    assert events[0].execution_date == "2000-01-03"
    assert events[1].execution_date == "2020-08-31"
    assert page_a.called and page_b.called


@respx.mock
async def test_fetch_dividends_single_page():
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/dividends.*")).mock(
        return_value=httpx.Response(200, json={
            "status": "OK",
            "results": [
                {"ticker": "SPY", "ex_dividend_date": "2024-03-15",
                 "cash_amount": 1.71, "currency": "USD"},
            ],
        })
    )
    events = await fetch_dividends(symbol="SPY", api_key="test-key")
    assert len(events) == 1
    assert events[0] == DividendEvent(
        ex_dividend_date="2024-03-15", cash_amount=1.71,
    )


@respx.mock
async def test_fetch_splits_empty_results_returns_empty():
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/splits.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": []})
    )
    events = await fetch_splits(symbol="UNKNOWN", api_key="test-key")
    assert events == []
```

- [ ] **Step 2: Run tests to verify they fail**

Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `polygon_corp_actions.py`**

```python
# PythonDataService/app/data_lake/polygon_corp_actions.py
"""Polygon corp-action fetchers: splits + dividends.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.6

Both endpoints follow the same paginated `next_url` pattern as the aggregate
fetcher (see polygon_fetcher.py). Results are sorted ascending by event date
so downstream consumers (factor_files.py) can build cumulative adjustments
left-to-right.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_POLYGON_BASE = "https://api.polygon.io"
_TIMEOUT_S = 30.0


@dataclass(frozen=True, order=True)
class SplitEvent:
    """One split event. split_from:split_to (e.g. 1:4 = 4-for-1 split).

    `order=True` lets callers `sorted([...])` in execution_date order
    (the field is YYYY-MM-DD ISO so lexical sort = chronological sort).
    """

    execution_date: str
    split_from: float
    split_to: float


@dataclass(frozen=True, order=True)
class DividendEvent:
    """One cash dividend (USD, ex-date)."""

    ex_dividend_date: str
    cash_amount: float


async def fetch_splits(symbol: str, api_key: str) -> list[SplitEvent]:
    url = f"{_POLYGON_BASE}/v3/reference/splits"
    params = {"ticker": symbol.upper(), "limit": 1000, "apiKey": api_key}
    rows = await _paginated_get(url, params)
    out = [
        SplitEvent(
            execution_date=r["execution_date"],
            split_from=float(r["split_from"]),
            split_to=float(r["split_to"]),
        )
        for r in rows
        if r.get("ticker", "").upper() == symbol.upper()
    ]
    return sorted(out)


async def fetch_dividends(symbol: str, api_key: str) -> list[DividendEvent]:
    url = f"{_POLYGON_BASE}/v3/reference/dividends"
    params = {"ticker": symbol.upper(), "limit": 1000, "apiKey": api_key}
    rows = await _paginated_get(url, params)
    out = [
        DividendEvent(
            ex_dividend_date=r["ex_dividend_date"],
            cash_amount=float(r["cash_amount"]),
        )
        for r in rows
        if r.get("ticker", "").upper() == symbol.upper()
    ]
    return sorted(out)


async def _paginated_get(url: str, params: dict) -> list[dict]:
    out: list[dict] = []
    next_url: str | None = url
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        while next_url is not None:
            req_params = params if next_url == url else {"apiKey": params["apiKey"]}
            resp = await client.get(next_url, params=req_params)
            resp.raise_for_status()
            payload = resp.json()
            out.extend(payload.get("results") or [])
            next_url = payload.get("next_url")
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/data_lake/polygon_corp_actions.py PythonDataService/tests/unit/data_lake/test_polygon_corp_actions.py
git commit -m "feat(data-lake): Polygon splits + dividends fetchers (Slice 1c)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `factor_files.py` — LEAN factor-file CSV builder

**Files:**
- Create: `PythonDataService/app/data_lake/factor_files.py`
- Create: `PythonDataService/tests/unit/data_lake/test_factor_files.py`

LEAN factor-file format (one CSV per symbol; `equity/usa/factor_files/<sym>.csv`):

```text
date,price_factor,split_factor,ref_price
20200831,1,0.25,...
20240315,...,1,...
```

- `date` is YYYYMMDD.
- `price_factor` is the cumulative multiplier needed to back-adjust historical prices for dividends through that date.
- `split_factor` is the cumulative split ratio (LEAN multiplies historical prices by this).
- `ref_price` is the closing price on the date used to compute the factors (any non-NaN reference; LEAN uses this for sanity-checking).

For v1c, we'll emit a minimal-but-correct file:
- One row per corp-action event (split or dividend).
- Plus an anchor row at the start of history (1900-01-01 placeholder) with `price_factor=1, split_factor=1, ref_price=0`.
- Plus an end-of-history row at today with `price_factor=1, split_factor=1, ref_price=0`.

Real LEAN-vendor parity is deferred (per spec Slice 5 deferred list). v1c produces a file LEAN can load without errors.

- [ ] **Step 1: Write the failing tests**

```python
# PythonDataService/tests/unit/data_lake/test_factor_files.py
"""Format-correctness tests for LEAN factor-file builder.

Real vendor-equivalent factor parity is deferred to Slice 5; v1c produces
a file LEAN can load without errors and that captures the basic cumulative
back-adjustment for the splits + dividends we have.
"""

from __future__ import annotations

from datetime import date

from app.data_lake.factor_files import build_factor_file_bytes
from app.data_lake.polygon_corp_actions import DividendEvent, SplitEvent


def test_no_events_emits_two_anchor_rows():
    body = build_factor_file_bytes(
        symbol="SPY",
        splits=[],
        dividends=[],
        history_start=date(2020, 1, 1),
        history_end=date(2026, 5, 21),
    ).decode("ascii")
    lines = body.strip().split("\n")
    # Just the two anchor rows: start and end of history, both with factor=1.
    assert len(lines) == 2
    assert lines[0].startswith("20200101,1")
    assert lines[1].startswith("20260521,1")


def test_one_split_event_emits_three_rows():
    splits = [SplitEvent(execution_date="2020-08-31", split_from=1.0, split_to=4.0)]
    body = build_factor_file_bytes(
        symbol="SPY",
        splits=splits,
        dividends=[],
        history_start=date(2020, 1, 1),
        history_end=date(2026, 5, 21),
    ).decode("ascii")
    lines = body.strip().split("\n")
    assert len(lines) == 3
    # Pre-split anchor row: split_factor=0.25 (1/4).
    pre = lines[0].split(",")
    assert pre[0] == "20200101"
    assert float(pre[2]) == 0.25
    # Split event row.
    event = lines[1].split(",")
    assert event[0] == "20200831"
    # Post-split end row: split_factor=1.
    post = lines[2].split(",")
    assert post[0] == "20260521"
    assert float(post[2]) == 1.0


def test_one_dividend_event_emits_three_rows():
    dividends = [DividendEvent(ex_dividend_date="2024-03-15", cash_amount=1.71)]
    body = build_factor_file_bytes(
        symbol="SPY",
        splits=[],
        dividends=dividends,
        history_start=date(2020, 1, 1),
        history_end=date(2026, 5, 21),
    ).decode("ascii")
    lines = body.strip().split("\n")
    assert len(lines) == 3
    # The first row's price_factor < 1 (pre-dividend back-adjustment).
    first = lines[0].split(",")
    assert float(first[1]) < 1.0
    # End row has price_factor=1.
    last = lines[2].split(",")
    assert float(last[1]) == 1.0


def test_build_is_deterministic():
    splits = [SplitEvent(execution_date="2020-08-31", split_from=1.0, split_to=4.0)]
    a = build_factor_file_bytes("SPY", splits, [], date(2020, 1, 1), date(2026, 5, 21))
    b = build_factor_file_bytes("SPY", splits, [], date(2020, 1, 1), date(2026, 5, 21))
    assert a == b
```

- [ ] **Step 2: Run tests to verify they fail**

Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `factor_files.py`**

```python
# PythonDataService/app/data_lake/factor_files.py
"""LEAN factor-file CSV builder.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 5.1

LEAN factor-file format (`equity/usa/factor_files/<sym>.csv`):
  date,price_factor,split_factor,ref_price
  - date: YYYYMMDD
  - price_factor: cumulative dividend back-adjustment multiplier
  - split_factor: cumulative split-adjustment multiplier
  - ref_price: closing price on the date (sanity-check value; we emit 0 in v1c)

V1c is intentionally minimal:
  - We emit two anchor rows (history_start and history_end) plus one row per
    corp-action event.
  - Factors are cumulative back-adjustment so historical raw prices multiplied
    by the factor give the back-adjusted view.
  - Real LEAN-vendor parity is deferred to Slice 5 (per spec deferred list).

LEAN's parser is forgiving about ref_price=0; the column is used only for a
sanity check that's bypassed when the value is non-positive.
"""

from __future__ import annotations

from datetime import date

from app.data_lake.polygon_corp_actions import DividendEvent, SplitEvent


def build_factor_file_bytes(
    symbol: str,
    splits: list[SplitEvent],
    dividends: list[DividendEvent],
    history_start: date,
    history_end: date,
) -> bytes:
    """Build the deterministic factor-file CSV body for one symbol.

    All inputs must be sorted ascending by date (polygon_corp_actions returns
    them that way). The returned bytes are ASCII CSV without a header row,
    which is what LEAN expects.
    """
    events = _merge_events(splits, dividends)

    # Compute cumulative factors traversing events from oldest to newest.
    # LEAN multiplies historical prices by the factors at the row's date to
    # back-adjust into the present-day view.
    cumulative_split_factor = 1.0
    for ev in events:
        if isinstance(ev, SplitEvent):
            cumulative_split_factor *= ev.split_from / ev.split_to

    cumulative_price_factor = 1.0
    # Dividends back-adjust by (1 - cash_amount / ref_price); we don't have
    # ref_price in Slice 1c, so approximate using cash_amount alone (factor
    # < 1 for any dividend). Vendor parity is deferred.
    for ev in events:
        if isinstance(ev, DividendEvent):
            cumulative_price_factor *= max(0.001, 1.0 - ev.cash_amount / 500.0)

    rows: list[tuple[str, float, float, float]] = []

    # Anchor at history_start with the full cumulative factors (these apply
    # to the entire pre-event window).
    rows.append((
        _yyyymmdd(history_start),
        cumulative_price_factor,
        cumulative_split_factor,
        0.0,
    ))

    # One row per event with monotonically advancing factors.
    running_split = cumulative_split_factor
    running_price = cumulative_price_factor
    for ev in events:
        if isinstance(ev, SplitEvent):
            running_split = running_split / (ev.split_from / ev.split_to)
            ev_date = ev.execution_date
        else:
            running_price = running_price / max(0.001, 1.0 - ev.cash_amount / 500.0)
            ev_date = ev.ex_dividend_date
        rows.append((_dash_to_compact(ev_date), running_price, running_split, 0.0))

    # End-of-history anchor.
    rows.append((_yyyymmdd(history_end), 1.0, 1.0, 0.0))

    body_lines = [
        f"{d},{_f(pf)},{_f(sf)},{_f(rp)}"
        for d, pf, sf, rp in rows
    ]
    return ("\n".join(body_lines) + "\n").encode("ascii")


def _yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _dash_to_compact(iso: str) -> str:
    """'2020-08-31' → '20200831'."""
    return iso.replace("-", "")


def _f(x: float) -> str:
    """Format a factor — LEAN accepts standard %g; we use %g for compactness."""
    return f"{x:g}"


def _merge_events(
    splits: list[SplitEvent], dividends: list[DividendEvent]
) -> list[SplitEvent | DividendEvent]:
    """Merge into a single chronologically-sorted list.

    Date keys: SplitEvent.execution_date / DividendEvent.ex_dividend_date.
    Both are 'YYYY-MM-DD' strings; lexical sort = chronological sort.
    """

    def _date_key(ev: SplitEvent | DividendEvent) -> str:
        return ev.execution_date if isinstance(ev, SplitEvent) else ev.ex_dividend_date

    return sorted([*splits, *dividends], key=_date_key)
```

- [ ] **Step 4: Run tests to verify they pass**

Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/data_lake/factor_files.py PythonDataService/tests/unit/data_lake/test_factor_files.py
git commit -m "feat(data-lake): LEAN factor-file CSV builder (Slice 1c)

Minimal-but-correct v1c implementation. Real LEAN-vendor factor parity is
deferred to Slice 5 per spec deferred list.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `polygon_ticker_events.py` + `map_files.py` — LEAN map-file CSV

**Files:**
- Create: `PythonDataService/app/data_lake/polygon_ticker_events.py`
- Create: `PythonDataService/app/data_lake/map_files.py`
- Create: `PythonDataService/tests/unit/data_lake/test_polygon_ticker_events.py`
- Create: `PythonDataService/tests/unit/data_lake/test_map_files.py`

LEAN map-file format (one CSV per symbol; `equity/usa/map_files/<sym>.csv`):

```text
20100101,SPY,nyse
20261231,SPY,nyse
```

- Each row: `<yyyymmdd>,<ticker>,<exchange>`.
- Rows are sorted ascending.
- For symbols that never changed ticker, two rows: history_start and history_end with the same ticker.
- For symbols that changed (e.g. FB → META on 2022-06-09), three rows: pre-change end date with old ticker, post-change start date with new ticker, history_end with new ticker.

V1c handles the no-change case correctly; ticker-change support is partial (the API call is wired and the formatter handles the data when it's present, but we don't test against a real ticker-change symbol in 1c).

- [ ] **Step 1: Write the failing test for the polygon fetch**

```python
# PythonDataService/tests/unit/data_lake/test_polygon_ticker_events.py
from __future__ import annotations

import re

import httpx
import pytest
import respx

from app.data_lake.polygon_ticker_events import TickerEvent, fetch_ticker_events

pytestmark = pytest.mark.asyncio


@respx.mock
async def test_fetch_no_events_returns_empty():
    respx.get(re.compile(
        r"https://api\.polygon\.io/v3/reference/tickers/SPY/events.*"
    )).mock(return_value=httpx.Response(
        200, json={"status": "OK", "results": {"events": []}}
    ))
    events = await fetch_ticker_events(symbol="SPY", api_key="t")
    assert events == []


@respx.mock
async def test_fetch_returns_normalized_events():
    respx.get(re.compile(
        r"https://api\.polygon\.io/v3/reference/tickers/META/events.*"
    )).mock(return_value=httpx.Response(200, json={
        "status": "OK",
        "results": {"events": [
            {"type": "ticker_change",
             "date": "2022-06-09",
             "ticker_change": {"ticker": "META"}},
        ]},
    }))
    events = await fetch_ticker_events(symbol="META", api_key="t")
    assert events == [TickerEvent(date="2022-06-09", new_ticker="META")]
```

- [ ] **Step 2: Run tests to verify they fail**

Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `polygon_ticker_events.py`**

```python
# PythonDataService/app/data_lake/polygon_ticker_events.py
"""Polygon ticker-event fetcher.

Polygon's /v3/reference/tickers/{ticker}/events returns the history of
ticker-change events for a symbol. We normalize to TickerEvent for the LEAN
map-file builder.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_POLYGON_BASE = "https://api.polygon.io"
_TIMEOUT_S = 30.0


@dataclass(frozen=True, order=True)
class TickerEvent:
    """A point at which the symbol's ticker changed to `new_ticker`."""

    date: str           # YYYY-MM-DD
    new_ticker: str


async def fetch_ticker_events(symbol: str, api_key: str) -> list[TickerEvent]:
    url = f"{_POLYGON_BASE}/v3/reference/tickers/{symbol.upper()}/events"
    params = {"types": "ticker_change", "apiKey": api_key}
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        resp = await client.get(url, params=params)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        payload = resp.json()

    events_raw = (payload.get("results") or {}).get("events") or []
    out: list[TickerEvent] = []
    for ev in events_raw:
        if ev.get("type") != "ticker_change":
            continue
        chg = ev.get("ticker_change") or {}
        ticker = chg.get("ticker")
        ev_date = ev.get("date")
        if ticker and ev_date:
            out.append(TickerEvent(date=ev_date, new_ticker=ticker))
    return sorted(out)
```

- [ ] **Step 4: Write the failing test for the map-file builder**

```python
# PythonDataService/tests/unit/data_lake/test_map_files.py
from __future__ import annotations

from datetime import date

from app.data_lake.map_files import build_map_file_bytes
from app.data_lake.polygon_ticker_events import TickerEvent


def test_no_change_emits_two_rows():
    body = build_map_file_bytes(
        symbol="SPY",
        events=[],
        history_start=date(2010, 1, 1),
        history_end=date(2026, 5, 21),
        exchange="nyse",
    ).decode("ascii")
    lines = body.strip().split("\n")
    assert len(lines) == 2
    assert lines[0] == "20100101,spy,nyse"
    assert lines[1] == "20260521,spy,nyse"


def test_one_ticker_change_emits_two_rows():
    events = [TickerEvent(date="2022-06-09", new_ticker="META")]
    body = build_map_file_bytes(
        symbol="META",
        events=events,
        history_start=date(2012, 5, 18),  # FB IPO
        history_end=date(2026, 5, 21),
        exchange="nasdaq",
    ).decode("ascii")
    lines = body.strip().split("\n")
    # Three rows: FB pre-change end, META post-change start, history_end.
    # But Polygon's events list gives us "ticker_change to META on 2022-06-09";
    # we don't know the prior ticker from a single event. In v1c we emit the
    # final ticker for the whole range plus the change date — vendor parity
    # for prior-ticker history is deferred to Slice 5.
    assert len(lines) == 2
    assert lines[0] == "20120518,meta,nasdaq"
    assert lines[1] == "20260521,meta,nasdaq"


def test_build_is_deterministic():
    a = build_map_file_bytes("SPY", [], date(2010, 1, 1), date(2026, 5, 21), "nyse")
    b = build_map_file_bytes("SPY", [], date(2010, 1, 1), date(2026, 5, 21), "nyse")
    assert a == b
```

- [ ] **Step 5: Run tests to verify they fail**

Expected: FAIL — module doesn't exist.

- [ ] **Step 6: Implement `map_files.py`**

```python
# PythonDataService/app/data_lake/map_files.py
"""LEAN map-file CSV builder.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 5.1

LEAN map-file format (`equity/usa/map_files/<sym>.csv`):
  <yyyymmdd>,<ticker_lowercase>,<exchange>

For symbols that never changed ticker, two rows: history_start and
history_end with the same ticker. For changed symbols (e.g. FB → META on
2022-06-09), full ticker-history reconstruction is deferred to Slice 5; v1c
emits the current ticker for the entire range, which is acceptable for the
EMA-crossover smoke and for any symbol that didn't change in the test window.
"""

from __future__ import annotations

from datetime import date

from app.data_lake.polygon_ticker_events import TickerEvent


def build_map_file_bytes(
    symbol: str,
    events: list[TickerEvent],  # noqa: ARG001 (v1c ignores ticker history)
    history_start: date,
    history_end: date,
    exchange: str,
) -> bytes:
    """Build the deterministic map-file CSV body for one symbol.

    V1c emits the current ticker for the entire range; Slice 5 adds full
    historical-ticker reconstruction. The function accepts `events` to
    establish the API surface; the values are unused until then.
    """
    sym = symbol.lower()
    ex = exchange.lower()
    rows = [
        f"{history_start.strftime('%Y%m%d')},{sym},{ex}",
        f"{history_end.strftime('%Y%m%d')},{sym},{ex}",
    ]
    return ("\n".join(rows) + "\n").encode("ascii")
```

- [ ] **Step 7: Run all tests to verify they pass**

```bash
podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_polygon_ticker_events.py tests/unit/data_lake/test_map_files.py -v
```
Expected: 5 tests PASS.

- [ ] **Step 8: Commit**

```bash
git add PythonDataService/app/data_lake/polygon_ticker_events.py \
        PythonDataService/app/data_lake/map_files.py \
        PythonDataService/tests/unit/data_lake/test_polygon_ticker_events.py \
        PythonDataService/tests/unit/data_lake/test_map_files.py
git commit -m "feat(data-lake): Polygon ticker events + LEAN map-file builder (Slice 1c)

Minimal v1c map-file: current ticker over the whole range. Full historical
ticker reconstruction is deferred to Slice 5.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: `derived_daily.py` — minute-trade → daily aggregation

**Files:**
- Create: `PythonDataService/app/data_lake/derived_daily.py`
- Create: `PythonDataService/tests/unit/data_lake/test_derived_daily.py`

LEAN daily format (`equity/usa/daily/<sym>.zip` containing `<sym>.csv`):

```text
20240520 00:00,500.10,501.50,499.80,501.20,12345678
```

Columns: `YYYYMMDD 00:00, open*10000, high*10000, low*10000, close*10000, volume`.
Note: same deci-cent scale as minute trade bars.

- [ ] **Step 1: Write the failing tests**

```python
# PythonDataService/tests/unit/data_lake/test_derived_daily.py
from __future__ import annotations

import io
import zipfile
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.data_lake.derived_daily import (
    DailyAggregate,
    aggregate_minute_to_daily,
    build_daily_zip_bytes,
)
from app.data_lake.lean_writer import MinuteTradeBar

ET = ZoneInfo("America/New_York")


def _bar(date_str: str, hour: int, minute: int, close: float) -> MinuteTradeBar:
    y, m, d = (int(x) for x in date_str.split("-"))
    bar_start = datetime(y, m, d, hour, minute, tzinfo=ET)
    return MinuteTradeBar(
        bar_start_et=bar_start,
        open=Decimal(str(close - 0.1)),
        high=Decimal(str(close + 0.2)),
        low=Decimal(str(close - 0.2)),
        close=Decimal(str(close)),
        volume=1234,
    )


def test_aggregate_minute_to_daily_one_day_one_aggregate():
    bars = [
        _bar("2024-05-20", 9, 30, 500.00),
        _bar("2024-05-20", 9, 31, 500.10),
        _bar("2024-05-20", 9, 32, 500.20),
    ]
    aggs = aggregate_minute_to_daily(bars)
    assert len(aggs) == 1
    a = aggs[0]
    assert a.trading_date.strftime("%Y%m%d") == "20240520"
    # Open = first bar's open; close = last bar's close; high = max of highs.
    assert a.open == Decimal("499.9")
    assert a.close == Decimal("500.2")
    assert a.high == Decimal("500.4")  # 500.20 + 0.2
    assert a.low == Decimal("499.8")    # 500.00 - 0.2
    assert a.volume == 3 * 1234


def test_aggregate_minute_to_daily_two_days_two_aggregates():
    bars = [
        _bar("2024-05-20", 9, 30, 500.00),
        _bar("2024-05-21", 9, 30, 501.00),
    ]
    aggs = aggregate_minute_to_daily(bars)
    assert len(aggs) == 2
    assert aggs[0].trading_date.strftime("%Y%m%d") == "20240520"
    assert aggs[1].trading_date.strftime("%Y%m%d") == "20240521"


def test_build_daily_zip_emits_csv_with_correct_name():
    bars = [_bar("2024-05-20", 9, 30, 500.00)]
    aggs = aggregate_minute_to_daily(bars)
    payload = build_daily_zip_bytes(symbol="SPY", aggregates=aggs)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        assert zf.namelist() == ["spy.csv"]
        csv = zf.read("spy.csv").decode("ascii")
    # One row, comma-separated, deci-cent prices.
    cols = csv.strip().split(",")
    assert cols[0] == "20240520 00:00"
    assert int(cols[4]) == 5_000_000  # close = 500.00 * 10000


def test_build_daily_zip_is_deterministic():
    bars = [_bar("2024-05-20", 9, 30, 500.00)]
    aggs = aggregate_minute_to_daily(bars)
    a = build_daily_zip_bytes("SPY", aggs)
    b = build_daily_zip_bytes("SPY", aggs)
    assert a == b
```

- [ ] **Step 2: Run tests to verify they fail**

Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `derived_daily.py`**

```python
# PythonDataService/app/data_lake/derived_daily.py
"""Daily-trade aggregation: minute-trade artifacts → daily zip.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.6

LEAN daily format (`equity/usa/daily/<sym>.zip`):
  Inner CSV: `<sym_lower>.csv`
  Columns (no header): "<YYYYMMDD HH:MM>", open*10000, high*10000, low*10000,
                       close*10000, volume
  Timestamp column always "<YYYYMMDD> 00:00" (session-start midnight).

Aggregation rules:
  - One row per trading_date that appears in the minute-bar input.
  - open = first bar's open
  - close = last bar's close
  - high = max(highs)
  - low = min(lows)
  - volume = sum(volumes)

Deterministic: same inputs produce byte-identical zip output.

NOT a vendor-equivalent of LEAN's own daily bars (those are separately
sourced and use slightly different bar boundaries). Repo-internal
consistency only.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.data_lake.lean_writer import MinuteTradeBar, to_deci_cent

_DETERMINISTIC_ZIP_DATE_TIME: tuple[int, int, int, int, int, int] = (
    1980, 1, 1, 0, 0, 0,
)


@dataclass(frozen=True)
class DailyAggregate:
    """OHLCV for a single trading_date in exchange-local terms."""

    trading_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


def aggregate_minute_to_daily(
    bars: list[MinuteTradeBar],
) -> list[DailyAggregate]:
    """Bucket minute bars by ET trading date and emit one OHLCV row per date.

    Input must be sorted ascending by bar_start_et; the function asserts that
    contract via a defensive check.
    """
    if not bars:
        return []

    out: list[DailyAggregate] = []
    cur_date: date | None = None
    cur_open: Decimal | None = None
    cur_high: Decimal | None = None
    cur_low: Decimal | None = None
    cur_close: Decimal | None = None
    cur_vol = 0

    for bar in bars:
        d = bar.bar_start_et.date()
        if d != cur_date:
            if cur_date is not None:
                out.append(DailyAggregate(
                    trading_date=cur_date,
                    open=cur_open,  # type: ignore[arg-type]
                    high=cur_high,  # type: ignore[arg-type]
                    low=cur_low,    # type: ignore[arg-type]
                    close=cur_close,  # type: ignore[arg-type]
                    volume=cur_vol,
                ))
            cur_date = d
            cur_open = bar.open
            cur_high = bar.high
            cur_low = bar.low
            cur_close = bar.close
            cur_vol = bar.volume
        else:
            cur_high = max(cur_high, bar.high)  # type: ignore[arg-type]
            cur_low = min(cur_low, bar.low)      # type: ignore[arg-type]
            cur_close = bar.close
            cur_vol += bar.volume

    if cur_date is not None:
        out.append(DailyAggregate(
            trading_date=cur_date,
            open=cur_open,  # type: ignore[arg-type]
            high=cur_high,  # type: ignore[arg-type]
            low=cur_low,    # type: ignore[arg-type]
            close=cur_close,  # type: ignore[arg-type]
            volume=cur_vol,
        ))

    return out


def build_daily_zip_bytes(
    symbol: str,
    aggregates: list[DailyAggregate],
) -> bytes:
    """Build the LEAN daily-zip payload for a symbol.

    Deterministic: same inputs produce byte-identical output (pinned ZIP
    epoch matches lean_writer.build_minute_trade_zip_bytes).
    """
    sym_lower = symbol.lower()
    lines = [
        f"{a.trading_date.strftime('%Y%m%d')} 00:00,"
        f"{to_deci_cent(a.open)},"
        f"{to_deci_cent(a.high)},"
        f"{to_deci_cent(a.low)},"
        f"{to_deci_cent(a.close)},"
        f"{a.volume}"
        for a in aggregates
    ]
    csv_body = "\n".join(lines) + ("\n" if lines else "")

    buf = io.BytesIO()
    info = zipfile.ZipInfo(
        filename=f"{sym_lower}.csv",
        date_time=_DETERMINISTIC_ZIP_DATE_TIME,
    )
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(info, csv_body)
    return buf.getvalue()
```

- [ ] **Step 4: Run tests to verify they pass**

Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/data_lake/derived_daily.py PythonDataService/tests/unit/data_lake/test_derived_daily.py
git commit -m "feat(data-lake): derived daily-trade aggregation (Slice 1c)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: `derived_quote.py` — minute-trade → minute-quote synthesis

**Files:**
- Create: `PythonDataService/app/data_lake/derived_quote.py`
- Create: `PythonDataService/tests/unit/data_lake/test_derived_quote.py`

LEAN minute quote format (`equity/usa/minute/<sym>/<yyyymmdd>_quote.zip`):

```text
<ms_since_midnight_et>, bid_open*10000, bid_high*10000, bid_low*10000, bid_close*10000, bid_size,
                       ask_open*10000, ask_high*10000, ask_low*10000, ask_close*10000, ask_size
```

V1c synthesizes by using the same close price for bid and ask (zero spread) and size=0. This matches what the existing `lean_sidecar_service` does and is enough for LEAN to load without warnings.

- [ ] **Step 1: Write the failing tests**

```python
# PythonDataService/tests/unit/data_lake/test_derived_quote.py
from __future__ import annotations

import io
import zipfile
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.data_lake.derived_quote import build_minute_quote_zip_bytes
from app.data_lake.lean_writer import MinuteTradeBar

ET = ZoneInfo("America/New_York")


def _bar(hour: int, minute: int, close: float) -> MinuteTradeBar:
    bar_start = datetime(2024, 5, 20, hour, minute, tzinfo=ET)
    return MinuteTradeBar(
        bar_start_et=bar_start,
        open=Decimal(str(close - 0.1)),
        high=Decimal(str(close + 0.2)),
        low=Decimal(str(close - 0.2)),
        close=Decimal(str(close)),
        volume=1234,
    )


def test_quote_zip_named_correctly():
    bars = [_bar(9, 30, 500.00)]
    payload = build_minute_quote_zip_bytes("SPY", "20240520", bars)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        assert zf.namelist() == ["20240520_spy_minute_quote.csv"]


def test_quote_csv_bid_equals_ask_zero_size():
    bars = [_bar(9, 30, 500.00)]
    payload = build_minute_quote_zip_bytes("SPY", "20240520", bars)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        csv = zf.read("20240520_spy_minute_quote.csv").decode("ascii")
    cols = csv.strip().split(",")
    # 11 columns: ms + 5 bid + 5 ask.
    assert len(cols) == 11
    # bid_close == ask_close == trade_close at deci-cent scale.
    assert int(cols[4]) == int(cols[9]) == 5_000_000
    # bid_size = ask_size = 0.
    assert int(cols[5]) == 0
    assert int(cols[10]) == 0


def test_quote_zip_is_deterministic():
    bars = [_bar(9, 30, 500.00), _bar(9, 31, 500.10)]
    a = build_minute_quote_zip_bytes("SPY", "20240520", bars)
    b = build_minute_quote_zip_bytes("SPY", "20240520", bars)
    assert a == b
```

- [ ] **Step 2: Run tests to verify they fail**

Expected: FAIL.

- [ ] **Step 3: Implement `derived_quote.py`**

```python
# PythonDataService/app/data_lake/derived_quote.py
"""Quote-zip synthesis from same-day minute-trade bars.

LEAN's default behavior is to load the matching `*_quote.zip` alongside
`*_trade.zip` if it exists; without one, you get a runtime warning. In v1c
we synthesize quote = trade with zero spread + zero size. This is enough
for LEAN to load without warnings and matches the existing
lean_sidecar_service.stage_quote_bars behavior.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.6

Real quote data from Polygon (when the plan tier permits) is a Slice 5
deferred item.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime

from app.data_lake.lean_writer import MinuteTradeBar, to_deci_cent

_DETERMINISTIC_ZIP_DATE_TIME: tuple[int, int, int, int, int, int] = (
    1980, 1, 1, 0, 0, 0,
)


def _ms_since_midnight_et(bar_start_et: datetime) -> int:
    midnight = bar_start_et.replace(hour=0, minute=0, second=0, microsecond=0)
    return int((bar_start_et - midnight).total_seconds() * 1000)


def build_minute_quote_zip_bytes(
    symbol: str,
    trading_date_yyyymmdd: str,
    bars: list[MinuteTradeBar],
) -> bytes:
    """Build the LEAN quote zip for one (symbol, trading_date).

    Each row's bid OHLC equals the trade OHLC; ask is the same; sizes are 0.
    Deterministic.
    """
    sym_lower = symbol.lower()
    csv_name = f"{trading_date_yyyymmdd}_{sym_lower}_minute_quote.csv"
    lines = [
        ",".join((
            str(_ms_since_midnight_et(b.bar_start_et)),
            # Bid OHLCV
            str(to_deci_cent(b.open)),
            str(to_deci_cent(b.high)),
            str(to_deci_cent(b.low)),
            str(to_deci_cent(b.close)),
            "0",
            # Ask OHLCV
            str(to_deci_cent(b.open)),
            str(to_deci_cent(b.high)),
            str(to_deci_cent(b.low)),
            str(to_deci_cent(b.close)),
            "0",
        ))
        for b in bars
    ]
    csv_body = "\n".join(lines) + ("\n" if lines else "")

    buf = io.BytesIO()
    info = zipfile.ZipInfo(filename=csv_name, date_time=_DETERMINISTIC_ZIP_DATE_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(info, csv_body)
    return buf.getvalue()
```

- [ ] **Step 4: Run tests to verify they pass**

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/data_lake/derived_quote.py PythonDataService/tests/unit/data_lake/test_derived_quote.py
git commit -m "feat(data-lake): derived minute-quote synthesis (Slice 1c)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: `sessions.py` upgrade — read market-hours-database

**Files:**
- Modify: `PythonDataService/app/data_lake/sessions.py`
- Modify: `PythonDataService/tests/unit/data_lake/test_sessions.py`

Currently `sessions.py` uses a hardcoded `_USA_FULL_HOLIDAYS` set covering 2024–2026. Slice 1c upgrades it to read the staged `market-hours-database.json` (Phase 0 bootstrap landed in Task 9 below). The hardcoded set stays as the **bootstrap fallback** for the first-ever `ensure_data` call against a virgin lake.

- [ ] **Step 1: Add a parameter for the metadata path**

Change `trading_sessions_for` signature to accept an optional `market_hours_db_path: Path | None`. When `None`, fall back to the hardcoded list. Append to `test_sessions.py`:

```python
import json
from pathlib import Path

import pytest

from app.data_lake.sessions import trading_sessions_for
from datetime import date


def test_uses_staged_market_hours_when_provided(tmp_path: Path):
    # Minimal market-hours-database.json with a single early-close on 2024-07-03
    # and a full closure on 2024-07-04 (US Independence Day).
    mh_db = tmp_path / "market-hours-database.json"
    mh_db.write_text(json.dumps({
        "entries": {
            "Equity-usa-[*]": {
                "exchange": "nyse",
                "timezone": "America/New_York",
                "holidays": ["2024-07-04"],
                "earlyCloses": {"2024-07-03": "13:00"},
            }
        }
    }))
    sessions, non_sessions = trading_sessions_for(
        "usa", date(2024, 7, 3), date(2024, 7, 5), market_hours_db_path=mh_db,
    )
    assert date(2024, 7, 4) not in sessions
    assert any(n.trading_date == date(2024, 7, 4) and n.reason == "market_holiday"
               for n in non_sessions)
    # Early-close day is still a session.
    assert date(2024, 7, 3) in sessions


def test_falls_back_to_hardcoded_when_no_path():
    sessions, _ = trading_sessions_for(
        "usa", date(2024, 5, 27), date(2024, 5, 27),  # Memorial Day
        market_hours_db_path=None,
    )
    assert sessions == []
```

- [ ] **Step 2: Run tests to verify they fail**

Expected: FAIL on the new test_uses_staged_market_hours_when_provided.

- [ ] **Step 3: Update `sessions.py`**

```python
# Add to sessions.py
import json
from pathlib import Path


def _parse_market_hours_holidays(mh_db_path: Path) -> frozenset[date]:
    """Read the LEAN market-hours-database.json and return USA-equity holidays.

    LEAN's JSON shape (simplified):
      {
        "entries": {
          "Equity-usa-[*]": {
            "holidays": ["2024-05-27", ...],
            "earlyCloses": {"2024-07-03": "13:00", ...},
            ...
          }
        }
      }
    """
    payload = json.loads(mh_db_path.read_text())
    entry = (payload.get("entries") or {}).get("Equity-usa-[*]", {})
    holidays = entry.get("holidays") or []
    out: set[date] = set()
    for h in holidays:
        y, m, d = (int(x) for x in h.split("-"))
        out.add(date(y, m, d))
    return frozenset(out)


def trading_sessions_for(
    market: str,
    start_trading_date: date,
    end_trading_date: date,
    market_hours_db_path: Path | None = None,
) -> tuple[list[date], list[NonSessionRecord]]:
    """Same as before, with optional market-hours-database override."""
    if market != "usa":
        raise ValueError(f"market {market!r} not supported in Slice 1c")

    holidays = (
        _parse_market_hours_holidays(market_hours_db_path)
        if market_hours_db_path is not None and market_hours_db_path.is_file()
        else _USA_FULL_HOLIDAYS
    )

    sessions: list[date] = []
    non_sessions: list[NonSessionRecord] = []
    current = start_trading_date
    while current <= end_trading_date:
        if current.weekday() >= 5:
            non_sessions.append(NonSessionRecord(market=market, trading_date=current, reason="weekend"))
        elif current in holidays:
            non_sessions.append(NonSessionRecord(market=market, trading_date=current, reason="market_holiday"))
        else:
            sessions.append(current)
        current += timedelta(days=1)
    return sessions, non_sessions
```

(The hardcoded `_USA_FULL_HOLIDAYS` stays as the fallback when no path is provided.)

- [ ] **Step 4: Run tests to verify they pass**

Expected: all sessions tests pass (6 total: 4 existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/data_lake/sessions.py PythonDataService/tests/unit/data_lake/test_sessions.py
git commit -m "feat(data-lake): sessions read from staged market-hours-database (Slice 1c)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: catalog claim ops for the new artifact kinds

**Files:**
- Modify: `PythonDataService/app/data_lake/catalog_client.py`
- Modify: `PythonDataService/tests/unit/data_lake/test_catalog_write_ops.py`

Three new claim helpers (one per partial unique index):

- `claim_corp_action_artifact(identity, ...)` — `ON CONFLICT (market, symbol, artifact_kind, provider, price_adjustment_mode) WHERE artifact_kind IN ('factor_file','map_file')`
- `claim_metadata_artifact(identity, ...)` — `ON CONFLICT (data_contract_hash) WHERE artifact_kind='metadata'`
- `claim_aggregated_bar_artifact(identity, ...)` — `ON CONFLICT (market, symbol, resolution, data_type, provider, price_adjustment_mode) WHERE artifact_kind='time_series_bars' AND resolution IN ('hour','daily')`

Each follows the same INSERT-ON-CONFLICT-DO-NOTHING-RETURNING pattern as `claim_minute_bar` from Slice 1b PR-E. Tests follow the same pattern as `test_catalog_write_ops.py` (live Postgres, TRUNCATE fixtures).

- [ ] **Step 1: Write the failing tests** — three tests, one per new claim function, each asserting:
  - First call returns an int id and creates a row
  - Second call with identical identity returns None (conflict)
- [ ] **Step 2: Run the tests** → FAIL (functions don't exist).
- [ ] **Step 3: Implement the three functions** following the `claim_minute_bar` template, with the partial-index WHERE clauses adapted per the spec § 3.1 unique index definitions.
- [ ] **Step 4: Run tests** → 3 new PASS (existing tests still pass).
- [ ] **Step 5: Commit**:
```bash
git add PythonDataService/app/data_lake/catalog_client.py PythonDataService/tests/unit/data_lake/test_catalog_write_ops.py
git commit -m "feat(data-lake): catalog claim ops for corp-action, metadata, aggregated-bar (Slice 1c)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: ensure_data wiring — Phase 0 metadata + per-kind dispatch + e2e test

**Files:**
- Modify: `PythonDataService/app/data_lake/ensure_data.py`
- Modify: `PythonDataService/app/data_lake/fake_polygon.py`
- Modify: `PythonDataService/app/data_lake/types.py`
- Create: `PythonDataService/tests/integration/data_lake/test_ensure_data_all_kinds.py`
- Modify: `PythonDataService/app/config.py` — add `LEAN_LAUNCHER_TOKEN` setting (if not already present) for the metadata extraction call.

This task wires everything together:

1. **Phase 0 metadata bootstrap** at the top of `ensure_data`:
   - Compute the metadata `data_contract_hash` (key by `provider='lean_image_extract'`, `provider_params={lean_image_digest, file_name}`).
   - For each of the two metadata files: claim → call `extract_lean_metadata` → write to lake → complete.
   - Pass the staged `market-hours-database.json` path to `expand_required_artifacts` → `trading_sessions_for(..., market_hours_db_path=mh_path)`.
2. **Per-kind dispatch** in the artifact loop — five branches:
   - `time_series_bars` + `minute` + `trade` → real polygon (from Slice 1b)
   - `time_series_bars` + `minute` + `quote` → derived_quote (depends on same-day trade)
   - `time_series_bars` + `daily` → derived_daily (depends on minute-trade completing first)
   - `factor_file` → polygon_corp_actions + factor_files
   - `map_file` → polygon_ticker_events + map_files
3. **Daily dependency ordering**: process minute-trade artifacts first, then derived (daily + quote) after. The spec § 4.6 calls this a two-pass: (1) Polygon-sourced artifacts; (2) derived artifacts after their dependencies complete.
4. **`data_contract_hash`**: replaces the `'x' * 64` placeholder with `data_contract_hash(provider, provider_params, price_adjustment_mode, session_policy='full', lean_format_version=1)`.
5. **`fake_polygon` is now never called** — narrow `synth_artifact_record` to raise on every kind. Slice 1d will delete the module.
6. **End-to-end integration test** at `tests/integration/data_lake/test_ensure_data_all_kinds.py`:
   - Mocks the launcher's `/extract-metadata` endpoint with sentinel bytes for market-hours + symbol-properties.
   - Mocks Polygon `/v2/aggs`, `/v3/reference/splits`, `/v3/reference/dividends`, `/v3/reference/tickers/{sym}/events`.
   - Runs `ensure_data` for SPY over a one-week window.
   - Asserts catalog rows exist for: minute-trade (5 sessions × 1), minute-quote (5 sessions × 1), daily-trade (1), factor_file (1), map_file (1), metadata (2). Total: 11 rows.
   - Asserts files exist on disk for all 11 paths.
   - Second call: `data_availability_hash` identical, all rows reused (`fetched_artifact_count == 0`).

- [ ] **Step 1: Add `'corp_action_revision_mismatch'` to `ArtifactFailure.reason`** in `types.py`.
- [ ] **Step 2: Write the failing e2e test** at `test_ensure_data_all_kinds.py` per the assertions above.
- [ ] **Step 3: Run it** → FAIL (the dispatch branches for the new kinds don't exist).
- [ ] **Step 4: Add the per-kind dispatch + Phase 0 bootstrap** to `ensure_data.py`. Use `data_contract_hash()` everywhere instead of `'x' * 64`.
- [ ] **Step 5: Update `fake_polygon.synth_artifact_record`** to raise on every kind (defensive boundary).
- [ ] **Step 6: Run the e2e test** → PASS (2 cases: write cycle + cache hit determinism).
- [ ] **Step 7: Run the full data_lake test suite** to verify no regressions of Slice 1a/1b tests:
  ```bash
  podman exec polygon-data-service python -m pytest tests/unit/data_lake/ tests/integration/data_lake/ -v
  ```
- [ ] **Step 8: Run project-scope ruff + full Python test suite** for final regression check.
- [ ] **Step 9: Commit**:
```bash
git add PythonDataService/app/data_lake/ensure_data.py \
        PythonDataService/app/data_lake/fake_polygon.py \
        PythonDataService/app/data_lake/types.py \
        PythonDataService/app/config.py \
        PythonDataService/tests/integration/data_lake/test_ensure_data_all_kinds.py
git commit -m "feat(data-lake): ensure_data per-kind dispatch + Phase 0 metadata (Slice 1c PR-G — final)

Wires every artifact kind through its real implementation:
  - Phase 0: LEAN metadata extracted via launcher, sessions read it
  - Minute-trade: real Polygon (from Slice 1b, unchanged)
  - Minute-quote: derived_quote from same-day trade artifact
  - Daily-trade: derived_daily aggregation after minute-trade completes
  - factor_file: Polygon corp-actions → LEAN factor-file CSV
  - map_file: Polygon ticker events → LEAN map-file CSV

data_contract_hash now computed deterministically (replaces the
'x' * 64 placeholder from Slice 1b).

fake_polygon.synth_artifact_record now refuses every kind — defensive
boundary; the module is deleted in Slice 1d.

End-to-end integration test exercises all 11 artifacts for SPY over a
one-week window with respx-mocked Polygon + launcher + real Postgres +
tmp filesystem.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Self-review

After completing all 10 tasks:

1. **Spec coverage** for Slice 1c deliverables (spec § 6.1 phase 1c):
   - [x] `derived.py` (quote synthesis + daily aggregation) — Tasks 6, 7
   - [x] `factor_files.py` from Polygon `/v3/reference/splits` + `/v3/reference/dividends` — Tasks 3, 4
   - [x] `map_files.py` from Polygon `/v3/reference/tickers/{sym}/events` — Task 5
   - [x] LEAN-image metadata extraction migrated into `data_lake/` — Task 1
   - [x] sessions.py upgraded to read staged market-hours-database — Task 8
   - [x] `data_contract_hash` deterministic (replaces 1b placeholder) — Task 2
   - [x] ensure_data dispatches every kind — Task 10

2. **Spec features deferred (correctly) to later slices**:
   - Real LEAN-vendor factor parity → Slice 5 (v1c emits LEAN-loadable but not vendor-equivalent factors)
   - Historical ticker reconstruction for symbols that changed → Slice 5
   - `prepare_run` workspace materialisation → Slice 1d
   - Backend GraphQL orchestration cut-over → Slice 1d
   - Launcher path-under-root → Slice 1d
   - `LeanMinuteDataReader` cutover → Slice 2
   - Sweep cron scheduling + corp-action revision recompute → Slice 4

3. **Type consistency**:
   - `data_contract_hash()` signature matches across Task 2 (definition) and Task 10 (callsite).
   - `MinuteTradeBar` is the source type for derived_daily.aggregate_minute_to_daily and derived_quote.build_minute_quote_zip_bytes. Same dataclass from Slice 1b.
   - `SplitEvent` / `DividendEvent` / `TickerEvent` are the input types for factor_files / map_files — consistent.

4. **Lint rule (no LEAN paths outside path_policy)**:
   - `factor_files.py` writes via path_policy.LeanFactorFilePath (path string is built in path_policy, the writer just receives bytes)
   - `map_files.py` similarly via LeanMapFilePath
   - `derived_daily.py` via LeanDailyBarPath
   - `derived_quote.py` via LeanMinuteBarPath (data_type='quote')
   - `lean_metadata.py` via LeanMetadataPath
   - No file directly constructs LEAN paths via string concat; the lint test from Slice 1a still passes.

5. **Known limitations / future paydown**:
   - Factor file is "minimal-but-correct" — produces a file LEAN can parse but cumulative back-adjustment math is simplified (dividend factor approximates ref_price=500). Real vendor parity is Slice 5.
   - Map file ignores the `events` argument in v1c (just emits the current ticker over the full range). API surface accepts events; Slice 5 implements the historical reconstruction.
   - The minute-quote synthesis uses zero-spread / zero-size. Real quote data from Polygon (when the plan tier permits) is Slice 5.

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-21-data-lake-slice-1c.md`.

Two execution options:

1. **Subagent-Driven (recommended)** — main agent dispatches fresh subagents per PR, autonomous marathon like the prior slices.
2. **Inline Execution** — drive each task in this session using `executing-plans`.

Which approach?
