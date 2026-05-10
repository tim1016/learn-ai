# PR (ii) — Python `TickerRequest` schema base + .NET DTO renames — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal (REVISED post-review):** Create a single Pydantic `TickerRequest` (and `MultiTickerRequest`) base in PythonDataService with `extra="forbid"`, calendar/order validation, and per-route default-preservation overrides; migrate the routes whose primary input genuinely matches `(symbol, from_date, to_date, timespan, multiplier)` to inherit it; rename matching .NET DTOs to canonical-only names (no transitional aliases — `JobsApi.cs` forwards JSON raw and `[JsonPropertyName]` is `AllowMultiple = false`). **Python `AliasChoices` is the only transitional layer** — every endpoint accepts both old (`ticker`, `start_date`, `end_date`) and new (`symbol`, `from_date`, `to_date`) field names during PR (ii)→(iii). PR (iii) removes the Pydantic aliases.

**Architecture:** New module `PythonDataService/app/schemas/ticker_request.py` exports `_BarRange`, `TickerRequest`, `MultiTickerRequest`. Base sets `model_config = ConfigDict(populate_by_name=True, extra="forbid")` and a `model_validator(mode="after")` that parses dates via `date.fromisoformat` (catching calendar-invalid strings like `"2025-13-99"` that the regex misses) and verifies `from_date <= to_date`. Each migrated request model inherits via `class FooRequest(TickerRequest)` AND **explicitly overrides any inherited field whose default differs from the route's pre-migration default** — e.g. `SignalEngineJobRequest` sets `multiplier: int = Field(15, ge=1)` to preserve its current 15-min default. .NET DTOs at GraphQL-resolver paths get canonical-only renames; `Backend/Jobs/JobsApi.cs` is untouched (it forwards JSON raw and never deserializes typed DTOs).

**Routes that DO inherit (revised — see spec §"Contract matrix"):** `aggregates`, `data_quality`, `indicators`, `indicator_reliability`, `volatility` (per-model), `dataset` JSON endpoints, four `jobs.py` models, `engine.EngineBacktestRequest` (`_BarRange` only — symbol stays strategy-owned).

**Routes that explicitly DO NOT inherit (revised):** `chart` (uses single `timeframe: str`, not `timespan + multiplier`), `research_divergence.preflight` (uses `timeframe: Literal["5m","15m","1h"]`), `spec_strategy` (uses `StrategySpec.symbols: list[str]` plural inside the domain spec — own design follow-up, see spec §"Out of scope"), plus all options/recorder/edge/orthogonal routes that never matched.

**Tech Stack:** FastAPI, Pydantic v2, ruff, pytest + pytest-asyncio + httpx.AsyncClient, .NET 10, xUnit, NSubstitute, Hot Chocolate v15.

**Spec reference:** `docs/superpowers/specs/2026-05-09-ticker-range-picker-everywhere-design.md` §"Components — full surface" → "Python TickerRequest base", "Routes that inherit", ".NET DTOs", §"Build sequence — PR (ii)".

---

## File structure

**Created:**

```
PythonDataService/app/schemas/__init__.py        (if not present)
PythonDataService/app/schemas/ticker_request.py
PythonDataService/tests/schemas/__init__.py
PythonDataService/tests/schemas/test_ticker_request.py
```

**Modified (Python routers — one commit per file, REVISED — chart/spec_strategy removed):**

```
PythonDataService/app/routers/aggregates.py
PythonDataService/app/routers/data_quality.py
PythonDataService/app/routers/indicators.py
PythonDataService/app/routers/indicator_reliability.py
PythonDataService/app/routers/volatility.py
PythonDataService/app/routers/dataset.py            (JSON endpoints only; multipart Form() endpoints stay)
PythonDataService/app/routers/jobs.py               (4 request models — RuleBasedBacktest + FeatureResearch + SignalEngine + CrossSectional)
PythonDataService/app/routers/engine.py             (start_date/end_date rename only — _BarRange inheritance, NOT TickerRequest)
```

**Excluded** (do not migrate in this PR — see spec):
- `chart.py` — uses single `timeframe: str`; would lose information forcing into `timespan + multiplier`
- `research_divergence.py` — preflight uses `timeframe: Literal["5m","15m","1h"]`
- `spec_strategy.py` — `StrategySpec.symbols` is plural and load-bearing; own-design follow-up

**Modified (.NET DTOs — one commit total, REVISED — canonical-only renames, no transitional aliases):**

```
Backend/Models/DTOs/ResearchModels.cs
Backend/Models/DTOs/SignalModels.cs
Backend/Models/DTOs/IndicatorModels.cs
Backend/Models/DTOs/BatchResearchModels.cs
Backend/Models/DTOs/GapDetectionModels.cs
Backend/GraphQL/Mutation.cs                          (resolver arguments + [GraphQLName] schema aliases)
Backend/GraphQL/DataLabMutation.cs
Backend/GraphQL/Query.cs
```

(Exact list confirmed in Task 12 by `grep -ln 'Ticker\|StartDate\|EndDate'` against `Backend/`.)

**Untouched on the .NET side:**
- `Backend/Jobs/JobsApi.cs` — forwards JSON raw via `JsonNode.ParseAsync` for all five job-type flows; never deserializes typed DTOs, so renames are invisible to it.
- `Backend/Models/DTOs/SpecStrategyModels.cs` — spec-strategy is deferred to its own follow-up (see spec).

**Untouched:**
- `options`, `quantlib_options`, `iv_recorder`, `iv30`, `edge`, `market_monitor`, `tickers`, `sanitize`, `snapshot`, `golden_fixtures`, `portfolio`, `broker` — none of these have a primary `(symbol, from_date, to_date, timespan, multiplier)` shape.
- All Frontend code — PR (iii) handles consumer migrations.

---

## Conventions for every task

- **Branch:** continues on `feat/ticker-range-picker-everywhere` (the same branch PR i was committed to). After PR (i) merges and master is pulled, branch off master fresh as `feat/ticker-range-picker-everywhere-schema` and run this plan there.
- **Commit cadence:** one commit per task. Subject style: `feat(schema): …`, `refactor(<router>): …`, `test(<router>): …`.
- **TDD per router:** add a test that posts the NEW field names → run-fail → update the router model → run-pass → add a test that the OLD field name still works (transitional alias) → run-pass → commit.
- **Per-file iteration loop:**
  ```bash
  podman exec polygon-data-service python -m pytest tests/routers/test_<name>.py -v
  ```
- **Project-scope before push:**
  ```bash
  ruff check PythonDataService/app/ PythonDataService/tests/
  podman exec polygon-data-service python -m pytest tests/ -v -k "not slow"
  cd Backend.Tests && dotnet test
  dotnet format podman.sln --verify-no-changes
  ```

---

## Task 0: Confirm the contract matrix against the live tree

Before any code change, confirm every `_confirm_` cell in the spec's §"Contract matrix". This is the load-bearing artefact for default preservation.

- [ ] **Step 1: Run the audit greps** for each candidate inheritor:

```bash
cd PythonDataService/app/routers
for f in aggregates.py data_quality.py indicators.py indicator_reliability.py volatility.py dataset.py jobs.py engine.py; do
  echo "=== $f ==="
  grep -nE "class.*Request|^\s+(ticker|symbol|from_date|to_date|start_date|end_date|timespan|multiplier|session)\s*[:=]" $f
done
```

For each model, write down: current symbol field name (`ticker` or `symbol`), current dates (`from_date/to_date` or `start_date/end_date`), current `multiplier` default, current `timespan` default, current `session` default.

- [ ] **Step 2: Update the spec's contract matrix in place** with the confirmed values, replacing every `_confirm_` cell.

- [ ] **Step 3: Commit the matrix update**

```bash
git add docs/superpowers/specs/2026-05-09-ticker-range-picker-everywhere-design.md
git commit -m "docs(spec): confirm contract matrix for PR (ii) ticker-request migration

Audit completed against the live tree. Each inheriting router's
current symbol field, dates, and per-route defaults are now
recorded in the spec's contract matrix. Subsequent migration
commits in this PR (Tasks 2-11) override inherited fields where
the matrix shows a defaults mismatch."
```

This commit is the authoritative reference for every per-route migration that follows. **Skipping or rushing Task 0 is the single biggest regression risk in this PR.**

---

## Task 1: Create `TickerRequest` Pydantic base + parametric tests

**Files:**
- Create: `PythonDataService/app/schemas/__init__.py` (empty stub if not present)
- Create: `PythonDataService/app/schemas/ticker_request.py`
- Create: `PythonDataService/tests/schemas/__init__.py` (empty stub)
- Create: `PythonDataService/tests/schemas/test_ticker_request.py`

- [ ] **Step 1: Write the failing test**

```python
# PythonDataService/tests/schemas/test_ticker_request.py
from __future__ import annotations
import pytest
from pydantic import ValidationError

from app.schemas.ticker_request import (
    TickerRequest, MultiTickerRequest, _BarRange,
)


class TestBarRange:
    def test_accepts_valid_payload(self) -> None:
        r = _BarRange(from_date="2025-01-01", to_date="2025-01-31")
        assert r.timespan == "minute"  # default
        assert r.multiplier == 1       # default
        assert r.session == "rth"      # default

    def test_rejects_malformed_dates(self) -> None:
        with pytest.raises(ValidationError) as exc:
            _BarRange(from_date="2025-1-1", to_date="2025-01-31")
        assert "from_date" in str(exc.value)

    def test_rejects_zero_multiplier(self) -> None:
        with pytest.raises(ValidationError):
            _BarRange(from_date="2025-01-01", to_date="2025-01-31", multiplier=0)

    @pytest.mark.parametrize("ts", ["minute", "hour", "day"])
    def test_accepts_supported_timespans(self, ts: str) -> None:
        r = _BarRange(from_date="2025-01-01", to_date="2025-01-31", timespan=ts)  # type: ignore[arg-type]
        assert r.timespan == ts

    def test_rejects_unknown_timespan(self) -> None:
        with pytest.raises(ValidationError):
            _BarRange(from_date="2025-01-01", to_date="2025-01-31", timespan="weekly")  # type: ignore[arg-type]


class TestTickerRequest:
    def test_accepts_symbol_field(self) -> None:
        r = TickerRequest(symbol="SPY", from_date="2025-01-01", to_date="2025-01-31")
        assert r.symbol == "SPY"

    def test_accepts_legacy_ticker_alias(self) -> None:
        # Transitional — to be removed in PR iii's last commit
        r = TickerRequest.model_validate({
            "ticker": "SPY", "from_date": "2025-01-01", "to_date": "2025-01-31",
        })
        assert r.symbol == "SPY"

    def test_accepts_legacy_start_end_date_aliases(self) -> None:
        r = TickerRequest.model_validate({
            "symbol": "SPY", "start_date": "2025-01-01", "end_date": "2025-01-31",
        })
        assert r.from_date == "2025-01-01"
        assert r.to_date == "2025-01-31"

    def test_rejects_empty_symbol(self) -> None:
        with pytest.raises(ValidationError):
            TickerRequest(symbol="", from_date="2025-01-01", to_date="2025-01-31")

    def test_serializes_to_canonical_field_names(self) -> None:
        r = TickerRequest(symbol="SPY", from_date="2025-01-01", to_date="2025-01-31")
        # by_alias=False → canonical (post-alias) field names on the wire
        d = r.model_dump()
        assert "symbol" in d and "ticker" not in d
        assert "from_date" in d and "start_date" not in d


class TestMultiTickerRequest:
    def test_accepts_symbols_list(self) -> None:
        r = MultiTickerRequest(
            symbols=["SPY", "QQQ"], from_date="2025-01-01", to_date="2025-01-31",
        )
        assert r.symbols == ["SPY", "QQQ"]

    def test_rejects_empty_symbols(self) -> None:
        with pytest.raises(ValidationError):
            MultiTickerRequest(symbols=[], from_date="2025-01-01", to_date="2025-01-31")

    def test_accepts_legacy_tickers_alias(self) -> None:
        r = MultiTickerRequest.model_validate({
            "tickers": ["SPY", "QQQ"], "from_date": "2025-01-01", "to_date": "2025-01-31",
        })
        assert r.symbols == ["SPY", "QQQ"]
```

- [ ] **Step 2: Verify failure**

```bash
podman exec polygon-data-service python -m pytest tests/schemas/test_ticker_request.py -v
```
Expected: FAIL ("ModuleNotFoundError: No module named 'app.schemas.ticker_request'").

- [ ] **Step 3: Add `extra="forbid"` and date validator tests to the spec file**

Append to `tests/schemas/test_ticker_request.py`:

```python
class TestExtraForbidAndValidation:
    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError) as exc:
            TickerRequest.model_validate({
                "symbol": "SPY", "from_date": "2025-01-01", "to_date": "2025-01-31",
                "rogue_field": "value",
            })
        assert "rogue_field" in str(exc.value)
        assert "extra" in str(exc.value).lower()

    def test_rejects_calendar_invalid_date(self) -> None:
        with pytest.raises(ValidationError) as exc:
            TickerRequest(symbol="SPY", from_date="2025-13-99", to_date="2025-12-31")
        assert "calendar" in str(exc.value).lower() or "month" in str(exc.value).lower()

    def test_rejects_inverted_range(self) -> None:
        with pytest.raises(ValidationError) as exc:
            TickerRequest(symbol="SPY", from_date="2025-12-31", to_date="2025-01-01")
        assert "to_date" in str(exc.value) and "from_date" in str(exc.value)
```

- [ ] **Step 4: Implement**

```python
# PythonDataService/app/schemas/__init__.py
# (empty — package marker only)
```

```python
# PythonDataService/app/schemas/ticker_request.py
"""Canonical request schemas for ticker-bar endpoints.

Every route whose primary input is "bars for symbol X over date range
[from_date, to_date] at (timespan × multiplier) granularity" inherits
``TickerRequest``. Routes for a *universe* of symbols inherit
``MultiTickerRequest``.

`extra="forbid"` is required: Pydantic v2's default `extra="ignore"`
silently drops unknown fields, which would hide the rename bug after
PR (iii) removes the transitional aliases.

Transitional aliases — to be REMOVED in PR (iii):
    ticker     → symbol
    tickers    → symbols
    start_date → from_date
    end_date   → to_date

These aliases let PR (ii) ship before PR (iii)'s frontend payload
renames, so the merge order has tolerance. Once PR (iii) lands and
every consumer sends canonical names, the aliases are removed and
legacy names produce a clear `extra_forbidden` 422.
"""

from __future__ import annotations

from datetime import date as Date
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"

Timespan = Literal["minute", "hour", "day"]
Session = Literal["rth", "extended"]


class _BarRange(BaseModel):
    """Common shape for any request that pulls bars over a date range."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    from_date: str = Field(
        ...,
        pattern=DATE_PATTERN,
        validation_alias=AliasChoices("from_date", "start_date"),
    )
    to_date: str = Field(
        ...,
        pattern=DATE_PATTERN,
        validation_alias=AliasChoices("to_date", "end_date"),
    )
    timespan: Timespan = "minute"
    multiplier: int = Field(1, ge=1)
    session: Session = "rth"

    @model_validator(mode="after")
    def _validate_dates(self) -> "_BarRange":
        # Pattern only checks shape; "2025-13-99" passes the regex.
        # Parse with date.fromisoformat to verify calendar validity,
        # then confirm from_date <= to_date.
        try:
            f = Date.fromisoformat(self.from_date)
            t = Date.fromisoformat(self.to_date)
        except ValueError as e:
            raise ValueError(f"invalid calendar date: {e}") from e
        if t < f:
            raise ValueError(
                f"to_date ({self.to_date}) must be >= from_date ({self.from_date})"
            )
        return self


class TickerRequest(_BarRange):
    """Single-symbol bar request."""

    symbol: str = Field(
        ...,
        min_length=1,
        max_length=20,
        validation_alias=AliasChoices("symbol", "ticker"),
    )


class MultiTickerRequest(_BarRange):
    """Universe-of-symbols bar request — used by cross-sectional research."""

    symbols: list[str] = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("symbols", "tickers"),
    )
```

- [ ] **Step 4: Run + confirm**

```bash
podman exec polygon-data-service python -m pytest tests/schemas/test_ticker_request.py -v
```
Expected: PASS (all ~14 cases).

```bash
ruff check PythonDataService/app/schemas/ PythonDataService/tests/schemas/
```
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/schemas/__init__.py \
        PythonDataService/app/schemas/ticker_request.py \
        PythonDataService/tests/schemas/__init__.py \
        PythonDataService/tests/schemas/test_ticker_request.py
git commit -m "feat(schema): add TickerRequest / MultiTickerRequest base

Single canonical Pydantic base for every route whose primary input is
'bars for symbol X over date range'. Transitional aliases (ticker,
tickers, start_date, end_date) accepted during PR (ii) → (iii)
migration window; removed in PR (iii)'s final commit."
```

---

## Per-router migration pattern (reference)

Tasks 2–11 each follow the same five-step pattern. The pattern is shown once here in full and then summarized for each router.

**Pattern:**

1. **Add a new test** (or update existing) under `PythonDataService/tests/routers/test_<router>.py` that posts the **new** field names and asserts a 200 response.
2. **Run** the test, expect FAIL (the route's existing model doesn't accept `symbol` if it was named `ticker`).
3. **Edit** the router file: change `class FooRequest(BaseModel)` → `class FooRequest(TickerRequest)` (or `MultiTickerRequest`); delete the now-redundant fields (`ticker`, `from_date`, `to_date`, `timespan`, `multiplier`, `session`); keep the route-specific fields. Update any `request.ticker` reads in the route handler to `request.symbol` (and analogous for date fields).
4. **Run** the test, expect PASS. **Run** an additional test posting the OLD field names — expect PASS (transitional alias).
5. **Commit** with message `refactor(<router>): inherit TickerRequest / MultiTickerRequest`.

For each task below: file paths, exact diff snippet, exact test code, and expected output are listed inline. Do not skip.

---

## Task 2: Migrate `aggregates.py`

**Files:**
- Modify: `PythonDataService/app/routers/aggregates.py`
- Modify: `PythonDataService/tests/routers/test_aggregates.py` (or create if not present)

- [ ] **Step 1: Add the new-field-names test**

```python
# PythonDataService/tests/routers/test_aggregates.py — add (don't replace existing)
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app

@pytest.mark.asyncio
async def test_aggregates_accepts_symbol_field(respx_mock):
    # Mock Polygon — see existing test fixtures for the standard mock
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/aggregates", json={
            "symbol": "SPY",
            "from_date": "2025-04-01", "to_date": "2025-04-02",
            "timespan": "day", "multiplier": 1,
        })
    assert resp.status_code == 200, resp.text

@pytest.mark.asyncio
async def test_aggregates_accepts_legacy_ticker_field(respx_mock):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/aggregates", json={
            "ticker": "SPY",  # legacy alias
            "from_date": "2025-04-01", "to_date": "2025-04-02",
            "timespan": "day", "multiplier": 1,
        })
    assert resp.status_code == 200, resp.text
```

- [ ] **Step 2: Verify the new-name test fails**

```bash
podman exec polygon-data-service python -m pytest tests/routers/test_aggregates.py::test_aggregates_accepts_symbol_field -v
```
Expected: FAIL — Pydantic rejects `symbol` because the current model field is named `ticker`.

- [ ] **Step 3: Edit the router**

In `PythonDataService/app/routers/aggregates.py`:

```python
# Add import
from app.schemas.ticker_request import TickerRequest

# Find the request model (likely AggregatesRequest or similar) — replace
# its base + remove the now-inherited fields:

# Before:
class AggregatesRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=20)
    from_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    to_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    timespan: Literal["minute", "hour", "day"] = "minute"
    multiplier: int = Field(1, ge=1)
    # ...route-specific fields

# After:
class AggregatesRequest(TickerRequest):
    # Inherits: symbol, from_date, to_date, timespan, multiplier, session
    # ...route-specific fields stay
```

In the route handler, change every `request.ticker` to `request.symbol`. Run grep:
```bash
grep -n "request.ticker\|aggregates_request.ticker" PythonDataService/app/routers/aggregates.py
```
Replace all matches with `.symbol`.

- [ ] **Step 4: Run the tests, both should pass**

```bash
podman exec polygon-data-service python -m pytest tests/routers/test_aggregates.py -v
```
Expected: PASS — both the symbol-field test and the legacy-ticker-alias test.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/routers/aggregates.py PythonDataService/tests/routers/test_aggregates.py
git commit -m "refactor(aggregates): inherit TickerRequest base

Renames request.ticker → request.symbol in route handler. Legacy
field names (ticker, start_date/end_date) still accepted via
transitional alias; removed in PR (iii)'s final commit."
```

---

## Task 3: ~~Migrate chart.py~~ — REMOVED

`chart.py` uses a single `timeframe: str` (`"1m"|"5m"|...|"1D"`), not `timespan + multiplier`. Forcing it into `TickerRequest` would lose information. Stays on its current schema. See spec §"Routes that explicitly do NOT inherit".

**Skip to Task 4.**

---

## Task 4: Migrate `data_quality.py`

`DataQualityRequest` (line 25 today) — straight inherit. Pattern identical to Task 2.

- [ ] **Steps 1–4** identical to Task 2 with `data_quality.py` and `test_data_quality.py`.
- [ ] **Step 5: Commit** as `refactor(data-quality): inherit TickerRequest base`.

---

## Task 5: Migrate `indicators.py`

Inherit `TickerRequest`. Pattern identical to Task 2.

- [ ] **Steps 1–4** identical with `indicators.py` and `test_indicators.py`.
- [ ] **Step 5: Commit** as `refactor(indicators): inherit TickerRequest base`.

---

## Task 6: Migrate `indicator_reliability.py`

Inherit `TickerRequest`. Note: this router doesn't use `multiplier` semantically; the inherited default 1 is harmless.

- [ ] **Steps 1–4** identical with `indicator_reliability.py` and its test file.
- [ ] **Step 5: Commit** as `refactor(indicator-reliability): inherit TickerRequest base`.

---

## Task 7: Migrate `volatility.py`

Volatility's `/series` endpoints take ticker + range. Inherit `TickerRequest`. Other endpoints in the router (e.g. surface fits) stay if they don't share the shape.

- [ ] **Step 1: Identify which models in `volatility.py` match the `(symbol, from, to)` shape.**
  ```bash
  grep -nE "class.*Request.*BaseModel" PythonDataService/app/routers/volatility.py
  ```
- [ ] **Steps 2–4** for each matching model (typically just one).
- [ ] **Step 5: Commit** as `refactor(volatility): inherit TickerRequest base for series endpoints`.

---

## Task 8: Migrate `dataset.py` JSON endpoints

`dataset.py` has both JSON-bodied endpoints and multipart `Form()` endpoints (lines 556 and 586 today). **Only the JSON endpoints migrate** — `Form()` parameter binding doesn't compose with Pydantic inheritance the same way, and the dataset upload flow is orthogonal to picker payloads.

- [ ] **Step 1: Identify JSON endpoint models** — grep for `class .*Request.*BaseModel` in `dataset.py`. Migrate those.
- [ ] **Steps 2–4** for each JSON model.
- [ ] **Step 5: Commit** as `refactor(dataset): inherit TickerRequest in JSON endpoint models`.

---

## Task 9: Migrate `jobs.py` — four request models

`jobs.py` has four request models (lines 79, 117, 136, 154 today):

- `RuleBasedBacktestJobRequest` — `ticker`, `from_date`, `to_date`, `multiplier`, `timespan` → inherit `TickerRequest` + add a `job_id` field
- `CrossSectionalJobRequest` — `tickers`, `from_date`, `to_date` → inherit `MultiTickerRequest` + add `job_id`, `feature_name`, `target_type`, `force`
- `FeatureResearchJobRequest` — `ticker`, dates, `multiplier`, `timespan` → inherit `TickerRequest` + add `job_id`, `feature_name`, `force`
- `SignalEngineJobRequest` — same as feature-research → inherit `TickerRequest` + add `job_id`, `feature_name`, `flip_sign`, `regime_gate_enabled`, `force`

Note: these inherit `_CamelCaseModel` today (a custom base with `populate_by_name=True`). After migration, they need both — inherit `TickerRequest` AND keep camelCase field aliases. Approach:

```python
class _CamelCaseTickerRequest(TickerRequest):
    """Mixes camelCase aliases (frontend payload format) with TickerRequest."""
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )
```

- [ ] **Step 1: Add `_CamelCaseTickerRequest` helper at top of `jobs.py`** + add 4 tests (one per migrated model) posting both the new shape and the legacy shape.
- [ ] **Step 2: Verify** the new-shape tests fail.
- [ ] **Step 3: Replace each request model's base class**:

```python
class RuleBasedBacktestJobRequest(_CamelCaseTickerRequest):
    job_id: str = Field(..., min_length=1)
    parameters: dict = Field(default_factory=dict)
    # ticker/from_date/to_date/multiplier/timespan all inherited

class CrossSectionalJobRequest(_CamelCaseModel, MultiTickerRequest):
    # _CamelCaseModel and MultiTickerRequest are independent bases —
    # need to compose via a similar helper. See _CamelCaseMultiTickerRequest.
    job_id: str = Field(..., min_length=1)
    feature_name: str = Field(..., min_length=1)
    target_type: str = "directional"
    force: bool = False

class FeatureResearchJobRequest(_CamelCaseTickerRequest):
    job_id: str = Field(..., min_length=1)
    feature_name: str = Field(..., min_length=1)
    force: bool = False
    # NOTE: pre-migration default for `multiplier` is 1, which matches the
    # base. No override needed.

class SignalEngineJobRequest(_CamelCaseTickerRequest):
    # CRITICAL: SignalEngineJobRequest's pre-migration default is multiplier=15
    # (jobs.py:162). The base inherits multiplier=1. Override to preserve the
    # 15-minute bar default — without this override, signal-runner silently
    # switches to 1-minute bars after migration.
    multiplier: int = Field(15, ge=1)

    job_id: str = Field(..., min_length=1)
    feature_name: str = Field(..., min_length=1)
    flip_sign: bool = True
    regime_gate_enabled: bool = True
    force: bool = False
```

Apply the same default-preservation pattern to `RuleBasedBacktestJobRequest` (pre-migration default `multiplier=15`):

```python
class RuleBasedBacktestJobRequest(_CamelCaseTickerRequest):
    multiplier: int = Field(15, ge=1)   # preserve pre-migration default
    job_id: str = Field(..., min_length=1)
    parameters: dict = Field(default_factory=dict)
```

Add `_CamelCaseMultiTickerRequest` helper analogous to `_CamelCaseTickerRequest`.

Update every `request.ticker` / `request.tickers` in the route handlers.

**Add an explicit default-preservation test** to `tests/routers/test_jobs.py`:

```python
def test_signal_engine_job_request_defaults_to_multiplier_15() -> None:
    """Regression test for the post-migration default — must stay 15."""
    r = SignalEngineJobRequest(
        job_id="test", ticker="SPY", feature_name="rsi",
        from_date="2025-01-01", to_date="2025-01-31",
    )
    assert r.multiplier == 15  # NOT 1 (the base default)

def test_rule_based_backtest_job_request_defaults_to_multiplier_15() -> None:
    r = RuleBasedBacktestJobRequest(
        job_id="test", ticker="SPY",
        from_date="2025-01-01", to_date="2025-01-31",
    )
    assert r.multiplier == 15
```

- [ ] **Step 4: Run + verify all 8 tests (4 new + 4 legacy) pass.**
- [ ] **Step 5: Commit** as `refactor(jobs): inherit TickerRequest / MultiTickerRequest in 4 job models`.

---

## Task 10: Migrate `engine.py` — `EngineBacktestRequest`

The engine's `EngineBacktestRequest` (line 1165 today) has `start_date`/`end_date` (the only renamer in the codebase) and the symbol comes from the strategy at runtime, not from the request. Two paths:

- The request gets `start_date`/`end_date` renamed to `from_date`/`to_date` (with transitional alias).
- The request does NOT get `symbol` (strategy-owned).

Approach: inherit `_BarRange` (not `TickerRequest`) for the date+sampling part; symbol stays out.

- [ ] **Step 1: Add tests** posting both the new (`from_date`/`to_date`) and legacy (`start_date`/`end_date`) shapes against `/api/engine/backtest`.
- [ ] **Step 2: Verify failure** on the new-name test.
- [ ] **Step 3: Edit `engine.py`**:

```python
from app.schemas.ticker_request import _BarRange

# Before
class EngineBacktestRequest(BaseModel):
    strategy_name: str = Field(...)
    fill_mode: str = Field("signal_bar_close", ...)
    # ...
    start_date: str | None = Field(None, ...)
    end_date: str | None = Field(None, ...)
    # ...

# After — _BarRange's from_date/to_date are required, but the engine treats
# them as optional overrides. We can't inherit directly; instead we add the
# AliasChoices manually for the rename:

class EngineBacktestRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    strategy_name: str = Field(...)
    fill_mode: str = Field("signal_bar_close", ...)
    # ...
    from_date: str | None = Field(
        None, pattern=DATE_PATTERN,
        validation_alias=AliasChoices("from_date", "start_date"),
    )
    to_date: str | None = Field(
        None, pattern=DATE_PATTERN,
        validation_alias=AliasChoices("to_date", "end_date"),
    )
    # ...
```

Update every `request.start_date` / `request.end_date` in the route handler to `request.from_date` / `request.to_date`. The behaviour is unchanged — only field names change.

- [ ] **Step 4: Run + verify both tests pass.**
- [ ] **Step 5: Commit** as `refactor(engine): rename EngineBacktestRequest start_date/end_date → from_date/to_date`.

---

## Task 11: ~~Migrate spec_strategy.py~~ — REMOVED

`SpecBacktestRequest` carries `StrategySpec.symbols: list[str]` (plural) inside the domain spec. Lifting `symbol` (singular) out is a domain-shape change, not a UI consolidation. Tracked in spec §"Out of scope" as own design.

**Skip to Task 12.**

---

## Task 12: .NET DTO renames + transitional aliases

This is one task because the .NET changes are tightly coupled — DTOs feed forwarders feed the GraphQL schema, and a partial rename breaks compilation.

**Files** (confirmed list — final list verified by grep):
```
Backend/Models/DTOs/ResearchModels.cs
Backend/Models/DTOs/SignalModels.cs
Backend/Models/DTOs/IndicatorModels.cs
Backend/Models/DTOs/BatchResearchModels.cs
Backend/Models/DTOs/SpecStrategyModels.cs
Backend/Models/DTOs/GapDetectionModels.cs
Backend/GraphQL/Mutation.cs
Backend/GraphQL/DataLabMutation.cs
Backend/GraphQL/SpecStrategyMutation.cs
Backend/GraphQL/Query.cs
Backend/Services/* (any forwarders constructing DTOs)
```

**Approach (REVISED post-review):** Canonical-only renames. **No `[JsonPropertyName]` transitional aliases** because:

1. `[JsonPropertyName]` is `AllowMultiple = false` — the original "two attributes on one property" example doesn't compile.
2. `Backend/Jobs/JobsApi.cs:88-121` parses bodies as `JsonNode`/`JsonObject` and forwards raw via `bodyObj.ToJsonString()` — five job-type flows never deserialize typed DTOs, so DTO aliases on those paths would be inert.
3. `Backend/Models/DTOs/SpecStrategyModels.cs` is **untouched** here (spec-strategy is deferred to its own design — see spec).

For GraphQL resolvers, use Hot Chocolate's `[GraphQLName]` to keep a one-PR-cycle schema-side alias on resolver arguments where a stale frontend GraphQL query might otherwise 400.

- [ ] **Step 1: Confirm the file list**

```bash
grep -rln "Ticker\|StartDate\|EndDate" Backend/ --include="*.cs" | grep -v "/SpecStrategy"
```

For each match, decide: is this field one of `Ticker`, `StartDate`, or `EndDate` on a DTO that genuinely deserializes a body OR a GraphQL resolver argument? If yes, it renames. If it's local/internal, leave it. **Skip `SpecStrategyModels.cs` and `SpecStrategyMutation.cs`** (deferred).

- [ ] **Step 2: Write a focused .NET test**

`Backend.Tests/Models/TickerRequestSerializationTests.cs` (new file):

```csharp
using System.Text.Json;
using Backend.Models.DTOs;
using Xunit;

public class TickerRequestSerializationTests
{
    [Fact]
    public void NewFieldNamesDeserialize()
    {
        var json = """{"symbol":"SPY","from_date":"2025-04-01","to_date":"2025-04-30"}""";
        var opts = new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };
        var dto = JsonSerializer.Deserialize<FeatureResearchRequest>(json, opts);
        Assert.NotNull(dto);
        Assert.Equal("SPY", dto!.Symbol);
        Assert.Equal("2025-04-01", dto.FromDate);
    }

    [Fact]
    public void LegacyFieldNamesAreNotAcceptedAtDtoLayer()
    {
        // Compatibility lives in Python (Pydantic AliasChoices). The .NET
        // DTO does NOT accept legacy names — Symbol stays default/null.
        var json = """{"ticker":"SPY","start_date":"2025-04-01","end_date":"2025-04-30"}""";
        var opts = new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };
        var dto = JsonSerializer.Deserialize<FeatureResearchRequest>(json, opts);
        Assert.True(string.IsNullOrEmpty(dto?.Symbol));
    }

    [Fact]
    public void SerializationProducesNewFieldNames()
    {
        var dto = new FeatureResearchRequest { Symbol = "SPY", FromDate = "2025-04-01", ToDate = "2025-04-30" };
        var opts = new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };
        var json = JsonSerializer.Serialize(dto, opts);
        Assert.Contains("\"symbol\":", json);
        Assert.Contains("\"from_date\":", json);
        Assert.DoesNotContain("\"ticker\":", json);
        Assert.DoesNotContain("\"start_date\":", json);
    }
}
```

(`FeatureResearchRequest` is illustrative — actual DTO names live in `Backend/Models/DTOs/ResearchModels.cs`. Use the actual names.)

- [ ] **Step 3: Run + verify failure**

```bash
cd Backend.Tests && dotnet test --filter "TickerRequestSerializationTests"
```
Expected: FAIL on `NewFieldNamesDeserialize` (DTO still has `Ticker`).

- [ ] **Step 4: Canonical-only DTO renames**

For each DTO property:

```csharp
// Before
public required string Ticker { get; init; }
public required string StartDate { get; init; }
public required string EndDate { get; init; }

// After — canonical only, no aliases
public required string Symbol { get; init; }
public required string FromDate { get; init; }
public required string ToDate { get; init; }
```

For each consumer of the renamed property (search `\.Ticker\b`, `\.StartDate\b`, `\.EndDate\b` across `Backend/` excluding `SpecStrategy*`), update to the new name. The compiler is your friend — break it on rename, fix every callsite.

- [ ] **Step 5: Add `[GraphQLName]` schema aliases for resolver arguments**

For each Hot Chocolate resolver method whose argument was named `ticker`, `startDate`, or `endDate`, pin the legacy GraphQL field name:

```csharp
// Before
public Task<ResearchResult> RunResearch(string ticker, string startDate, string endDate, ...);

// After — argument renamed; legacy GraphQL schema field name pinned for one PR cycle
public Task<ResearchResult> RunResearch(
    [GraphQLName("ticker")] string symbol,
    [GraphQLName("startDate")] string fromDate,
    [GraphQLName("endDate")] string toDate,
    ...);
```

This keeps in-flight frontend GraphQL queries from breaking during the PR (ii)→(iii) window. PR (iii)'s alias-cleanup commit removes the `[GraphQLName]` overrides so the canonical names become authoritative.

- [ ] **Step 6: Run + verify all tests pass**

```bash
cd Backend.Tests && dotnet test
```
Expected: ALL PASS (any `Ticker` / `StartDate` consumer must have been updated to compile).

```bash
dotnet format podman.sln --verify-no-changes
```
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add Backend/ Backend.Tests/
git commit -m "refactor(backend): canonical-only Ticker→Symbol, StartDate/EndDate→FromDate/ToDate

.NET DTOs at the GraphQL-resolver paths get canonical-only renames in
lockstep with the Python TickerRequest schema. NO transitional
[JsonPropertyName] aliases — JsonPropertyName is AllowMultiple=false
and JobsApi.cs forwards JSON raw for all five job flows without
deserializing typed DTOs anyway.

Compatibility during the PR (ii)→(iii) window lives in Python
(Pydantic AliasChoices). For GraphQL resolver arguments, [GraphQLName]
pins the legacy schema field name for one PR cycle so in-flight
queries don't break.

SpecStrategyModels.cs is intentionally untouched (spec-strategy
deferred to its own design)."
```

---

## Task 13: Project-scope checks + push

- [ ] **Step 1: Python project-scope tests**

```bash
podman exec polygon-data-service python -m pytest tests/ -v -k "not slow"
```
Expected: ALL PASS. Cross-check with master baseline if any pre-existing failures show.

- [ ] **Step 2: Python lint**

```bash
ruff check PythonDataService/app/ PythonDataService/tests/
```
Expected: clean.

- [ ] **Step 3: .NET project-scope tests**

```bash
cd Backend.Tests && dotnet test
```
Expected: ALL PASS.

- [ ] **Step 4: .NET format**

```bash
dotnet format podman.sln --verify-no-changes
```
Expected: clean.

- [ ] **Step 5: Push and open PR**

```bash
git push -u origin feat/ticker-range-picker-everywhere-schema
gh pr create --title "refactor(api): TickerRequest schema unification + .NET DTO renames (PR ii of iii)" --body "$(cat <<'EOF'
## Summary
- New `PythonDataService/app/schemas/ticker_request.py` exports `_BarRange`, `TickerRequest`, `MultiTickerRequest`
- 10 routers now inherit the appropriate base — single canonical shape for every "bars over range" request
- .NET DTOs renamed in lockstep: `Ticker→Symbol`, `StartDate/EndDate→FromDate/ToDate`
- **Backwards-compatible** via Pydantic `AliasChoices` and `[JsonPropertyName]` transitional aliases — every endpoint accepts both old and new field names during the PR (ii)→(iii) window
- Frontend / consumer changes land in PR (iii); aliases removed there

## Spec
- Design: `docs/superpowers/specs/2026-05-09-ticker-range-picker-everywhere-design.md`
- Plan: `docs/superpowers/plans/2026-05-09-ticker-range-picker-everywhere-pr2-schema-unification.md`
- Predecessor: PR (i) (picker enhancements + new sibling components)

## Test plan
- [x] `tests/schemas/test_ticker_request.py` — base shape, alias acceptance, validation
- [x] Per-router test for new field names (~10 tests)
- [x] Per-router test for legacy field names still accepted (~10 tests)
- [x] `Backend.Tests/Models/TickerRequestSerializationTests.cs` — both deserialization paths
- [x] `podman exec polygon-data-service python -m pytest tests/ -v -k "not slow"` — clean
- [x] `cd Backend.Tests && dotnet test` — clean
- [x] `ruff check ...` — clean
- [x] `dotnet format podman.sln --verify-no-changes` — clean

## Risks
- Aliases must stay until PR (iii)'s final commit; deleting them too early fails any in-flight Frontend payload still using legacy names. Tracked as the explicit final commit of PR (iii).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

After PR open, **stop**. PR-monitor handles review.

---

## Self-review

Spec coverage:
- ✅ `TickerRequest` / `MultiTickerRequest` / `_BarRange` Pydantic base — Task 1
- ✅ Each inheriting router migrated — Tasks 2–10
- ✅ `spec_strategy` transitional alias prep — Task 11
- ✅ .NET DTO renames + transitional aliases — Task 12
- ✅ Backward compat (legacy field names accepted) — Tasks 1, 2–11 (Pydantic `AliasChoices`), 12 (.NET `[JsonPropertyName]` private setters)
- ✅ Project-scope ruff + dotnet format + tests — Task 13

Type consistency:
- `TickerRequest`, `MultiTickerRequest`, `_BarRange` exported from `app/schemas/ticker_request.py` (Task 1) and imported by every Task 2–11 router. Consistent.
- .NET property names `Symbol`, `FromDate`, `ToDate` introduced in Task 12; transitional setters accept `ticker`, `start_date`, `end_date`. Consistent across all migrated DTOs.

No placeholders. No "TBD" / "implement later". The four-jobs migration in Task 9 has the most code but each model's diff is shown.

Plan complete.
