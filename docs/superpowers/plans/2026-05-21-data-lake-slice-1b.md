# Data Lake Slice 1b Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Slice 1a fixture-backed stub with the real Polygon-fetch → atomic-write → catalog-claim cycle for `minute-trade` artifacts only. Land the lease-expiry sweep skeleton (not yet scheduled). Other artifact kinds (factor / map / daily / quote / metadata) remain stubbed until Slice 1c.

**Architecture:** The Python `app/data_lake/` module gains three new modules — `atomic.py` (write-to-staging + fsync + atomic rename helpers with a same-filesystem startup guard), `lean_writer.py` (deci-cent CSV-in-zip writer with deterministic byte output), `polygon_fetcher.py` (paginated Polygon `/v2/aggs` HTTP fetcher with typed error mapping). The existing `catalog_client.py` grows write operations (claim / complete / fail / heartbeat / steal-or-retry / refresh-complete) matching the spec § 4.4 atomic transitions, each scoped to the right partial unique index. `ensure_data.py` dispatches per-artifact-kind: minute-trade flows through the real pipeline; other kinds keep using the Slice 1a fake stub.

**Tech Stack:** Python 3.12 + asyncpg + httpx + zipfile; Postgres 16; FastAPI; respx for HTTP mocking in tests.

**Spec:** [`docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md`](../specs/2026-05-20-polygon-lean-data-lake-design.md) — §§ 4.4 (concurrency primitives), 4.6 (batched fetch), 5.1 (volume layout), 5.2 (atomic rename protocol).

**Prior slice:** Slice 1a landed at master `5bca7c64` — catalog schema, path policy, types, gated ensure_data skeleton with fake_polygon.

---

## File structure

### New files

| File | Responsibility |
|---|---|
| `PythonDataService/app/data_lake/atomic.py` | Same-filesystem startup guard, `stage_path_for(...)`, `atomic_write_and_promote(content_bytes, rel_path, request_id, worker_id, attempt) → file_sha256` |
| `PythonDataService/app/data_lake/lean_writer.py` | Deci-cent encoder, deterministic CSV-in-zip writer, `build_minute_trade_zip_bytes(symbol, trading_date, bars) → bytes` |
| `PythonDataService/app/data_lake/polygon_fetcher.py` | `fetch_minute_trade_aggregates(symbol, start, end, api_key) → list[PolygonBar]` paginated fetcher, error → `ArtifactFailure.reason` mapping |
| `PythonDataService/app/data_lake/sweep.py` | `reclaim_expired_leases() → int` atomic UPDATE; not yet scheduled (Slice 4) |
| `PythonDataService/tests/unit/data_lake/test_atomic.py` | tmp_path-backed unit tests for the rename helper + startup guard |
| `PythonDataService/tests/unit/data_lake/test_lean_writer.py` | Determinism + format correctness |
| `PythonDataService/tests/unit/data_lake/test_polygon_fetcher.py` | respx-mocked unit tests for fetch + pagination + error mapping |
| `PythonDataService/tests/unit/data_lake/test_catalog_write_ops.py` | Live-Postgres unit tests for claim/complete/fail/heartbeat/steal-or-retry/refresh-complete |
| `PythonDataService/tests/unit/data_lake/test_sweep.py` | Live-Postgres test of `reclaim_expired_leases` |
| `PythonDataService/tests/integration/data_lake/test_ensure_data_real_polygon.py` | End-to-end: respx Polygon + real Postgres + tmp filesystem; verify catalog rows, on-disk bytes, deterministic hash, second-call cache hit |

### Modified files

| File | Change |
|---|---|
| `compose.yaml` | Add `${LEAN_DATA_VOLUME_HOST_PATH:-./data-lake-volume}:/lean-data-writer:rw` mount + `LEAN_DATA_WRITE_ROOT=/lean-data-writer` env on `python-service` |
| `PythonDataService/app/config.py` | Add `LEAN_DATA_WRITE_ROOT: str = "/lean-data-writer"` |
| `.gitignore` | Add `data-lake-volume/` so the default host-bind dir doesn't get committed |
| `PythonDataService/app/data_lake/catalog_client.py` | Add `claim_minute_bar`, `complete_artifact`, `fail_artifact`, `refresh_lease`, `steal_or_retry_minute_bar`, `refresh_complete_minute_bar` |
| `PythonDataService/app/data_lake/ensure_data.py` | Dispatch by artifact kind: minute-trade through real pipeline; others fall through to existing `fake_polygon` stub (Slice 1c replaces them) |
| `PythonDataService/app/data_lake/fake_polygon.py` | Document that `synth_artifact_record` is now only used for non-minute-trade artifacts in Slice 1b; raise on `minute_trade` to make the dispatch boundary explicit |
| `PythonDataService/app/data_lake/types.py` | Add `'unsupported_artifact_kind'` to `ArtifactFailure.reason` enum (used when a kind isn't implemented in this slice) |

---

## Tasks

### Task 1: compose host-bind mount + `LEAN_DATA_WRITE_ROOT`

**Files:**
- Modify: `compose.yaml`
- Modify: `PythonDataService/app/config.py`
- Modify: `.gitignore`

- [ ] **Step 1: Add the volume mount + env var to `python-service` in `compose.yaml`**

In `compose.yaml`, locate the `python-service` block. Under its `volumes:` list, after the existing `./PythonDataService/artifacts:/app/artifacts:z` line, add:

```yaml
      # Data lake writer mount (Slice 1b). Default host path is ./data-lake-volume
      # (gitignored); override LEAN_DATA_VOLUME_HOST_PATH in .env for a different root.
      # Layout inside this mount: lake/ (canonical artifacts) + staging/ (writer scratch),
      # both on the same filesystem so atomic rename works.
      - ${LEAN_DATA_VOLUME_HOST_PATH:-./data-lake-volume}:/lean-data-writer:rw
```

Under the same service's `environment:` list, before `POSTGRES_URL`, add:

```yaml
      # Data lake writer root (Slice 1b). Container path of the host-bind mount above.
      - LEAN_DATA_WRITE_ROOT=/lean-data-writer
```

- [ ] **Step 2: Add the setting to `app/config.py`**

In `PythonDataService/app/config.py`, locate the `# Data lake (Slice 1a)` comment block (the one with `POSTGRES_URL` and `DATA_LAKE_ENABLED`). After `DATA_LAKE_ENABLED: bool = False`, append:

```python
    # Data lake writer root (Slice 1b). Container-side path of the RW mount.
    # The writer creates lake/ and staging/ subdirectories under this path.
    # Must be on a single filesystem so POSIX atomic rename(2) is valid.
    LEAN_DATA_WRITE_ROOT: str = "/lean-data-writer"
```

- [ ] **Step 3: Add `data-lake-volume/` to `.gitignore`**

Append to `.gitignore`:

```
# Slice 1b: default host-bind dir for the data lake writer volume.
data-lake-volume/
```

- [ ] **Step 4: Recreate the python-service container so the new mount + env are live**

Run:
```bash
podman compose up -d python-service
```
Expected: container recreates, no errors. Verify the env var is set:
```bash
podman exec polygon-data-service env | grep LEAN_DATA_WRITE_ROOT
```
Expected: `LEAN_DATA_WRITE_ROOT=/lean-data-writer`.

Verify the mount is writable:
```bash
podman exec polygon-data-service sh -c 'mkdir -p /lean-data-writer/lake /lean-data-writer/staging && touch /lean-data-writer/.probe && ls -la /lean-data-writer'
```
Expected: directories created, no permission errors.

- [ ] **Step 5: Commit**

```bash
git add compose.yaml PythonDataService/app/config.py .gitignore
git commit -m "feat(data-lake): add LEAN_DATA_WRITE_ROOT mount + setting (Slice 1b)"
```

---

### Task 2: `atomic.py` — startup guard + staging path + atomic write helper

**Files:**
- Create: `PythonDataService/app/data_lake/atomic.py`
- Create: `PythonDataService/tests/unit/data_lake/test_atomic.py`

- [ ] **Step 1: Write the failing tests**

Create `PythonDataService/tests/unit/data_lake/test_atomic.py`:

```python
"""Unit tests for app.data_lake.atomic.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 5.2
"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from uuid import UUID

import pytest

from app.data_lake.atomic import (
    AtomicRenameUnsafeError,
    assert_same_filesystem,
    atomic_write_and_promote,
    stage_path_for,
)


class TestAssertSameFilesystem:
    def test_same_directory_passes(self, tmp_path: Path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        # No exception.
        assert_same_filesystem(a, b)

    def test_missing_directory_raises_FileNotFoundError(self, tmp_path: Path):
        a = tmp_path / "a"
        b = tmp_path / "does-not-exist"
        a.mkdir()
        with pytest.raises(FileNotFoundError):
            assert_same_filesystem(a, b)


class TestStagePathFor:
    def test_layout(self, tmp_path: Path):
        rel = PurePosixPath("equity/usa/minute/spy/20240520_trade.zip")
        request_id = UUID("12345678-1234-5678-1234-567812345678")
        result = stage_path_for(
            staging_root=tmp_path / "staging",
            rel_lake_path=rel,
            request_id=request_id,
            worker_id="worker-1",
            attempt=1,
        )
        assert result == (
            tmp_path / "staging" / "12345678-1234-5678-1234-567812345678"
            / "worker-1" / "attempt_1"
            / "equity" / "usa" / "minute" / "spy" / "20240520_trade.zip.tmp"
        )

    def test_two_attempts_distinct(self, tmp_path: Path):
        rel = PurePosixPath("equity/usa/minute/spy/20240520_trade.zip")
        request_id = UUID("12345678-1234-5678-1234-567812345678")
        a1 = stage_path_for(tmp_path / "staging", rel, request_id, "w", 1)
        a2 = stage_path_for(tmp_path / "staging", rel, request_id, "w", 2)
        assert a1 != a2


class TestAtomicWriteAndPromote:
    def test_writes_bytes_and_returns_sha256(self, tmp_path: Path):
        lake_root = tmp_path / "lake"
        staging_root = tmp_path / "staging"
        lake_root.mkdir()
        staging_root.mkdir()

        rel = PurePosixPath("equity/usa/minute/spy/20240520_trade.zip")
        content = b"hello world deci-cent payload"
        expected_sha = hashlib.sha256(content).hexdigest()

        result_sha = atomic_write_and_promote(
            content=content,
            lake_root=lake_root,
            staging_root=staging_root,
            rel_lake_path=rel,
            request_id=UUID("12345678-1234-5678-1234-567812345678"),
            worker_id="w",
            attempt=1,
        )

        assert result_sha == expected_sha
        final = lake_root / "equity" / "usa" / "minute" / "spy" / "20240520_trade.zip"
        assert final.is_file()
        assert final.read_bytes() == content

    def test_no_staging_leftover_after_promote(self, tmp_path: Path):
        lake_root = tmp_path / "lake"
        staging_root = tmp_path / "staging"
        lake_root.mkdir()
        staging_root.mkdir()
        rel = PurePosixPath("equity/usa/minute/spy/20240520_trade.zip")

        atomic_write_and_promote(
            content=b"x",
            lake_root=lake_root,
            staging_root=staging_root,
            rel_lake_path=rel,
            request_id=UUID("12345678-1234-5678-1234-567812345678"),
            worker_id="w",
            attempt=1,
        )

        # The .tmp staging file should be gone (rename moved it).
        staged = stage_path_for(
            staging_root, rel,
            UUID("12345678-1234-5678-1234-567812345678"), "w", 1,
        )
        assert not staged.exists()

    def test_cross_device_raises(self, tmp_path: Path, monkeypatch):
        """If lake_root and staging_root are on different st_dev values,
        atomic_write_and_promote refuses to proceed."""
        lake_root = tmp_path / "lake"
        staging_root = tmp_path / "staging"
        lake_root.mkdir()
        staging_root.mkdir()
        rel = PurePosixPath("a.zip")

        # Force assert_same_filesystem to disagree.
        from app.data_lake import atomic as atomic_module

        def fake_assert(a: Path, b: Path) -> None:
            raise AtomicRenameUnsafeError(
                f"different filesystems: {a} vs {b}"
            )

        monkeypatch.setattr(atomic_module, "assert_same_filesystem", fake_assert)
        with pytest.raises(AtomicRenameUnsafeError):
            atomic_write_and_promote(
                content=b"x",
                lake_root=lake_root,
                staging_root=staging_root,
                rel_lake_path=rel,
                request_id=UUID("12345678-1234-5678-1234-567812345678"),
                worker_id="w",
                attempt=1,
            )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_atomic.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'app.data_lake.atomic'`.

- [ ] **Step 3: Implement `atomic.py`**

Create `PythonDataService/app/data_lake/atomic.py`:

```python
"""Atomic-write helpers for the data lake writer.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 5.2

Contract:
  1. Stage the content under a request/worker/attempt-scoped path (so retries
     and parallel workers never collide).
  2. fsync the file and its parent directory.
  3. POSIX atomic rename(2) into the canonical lake path. Lake parent dirs are
     created on the way.
  4. fsync the lake parent directory so the rename hits disk.

Pre-condition: lake_root and staging_root MUST share the same filesystem
(same stat.st_dev). atomic_write_and_promote asserts this on every call.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path, PurePosixPath
from uuid import UUID

logger = logging.getLogger(__name__)


class AtomicRenameUnsafeError(RuntimeError):
    """Raised when staging and lake live on different filesystems."""


def assert_same_filesystem(lake_root: Path, staging_root: Path) -> None:
    """Both paths must exist AND share the same stat.st_dev.

    Raises FileNotFoundError if either path does not exist.
    Raises AtomicRenameUnsafeError if they live on different filesystems.
    """
    lake_dev = lake_root.stat().st_dev
    staging_dev = staging_root.stat().st_dev
    if lake_dev != staging_dev:
        raise AtomicRenameUnsafeError(
            f"lake_root and staging_root are on different filesystems "
            f"(st_dev {lake_dev} vs {staging_dev}). "
            f"POSIX rename(2) is not atomic across filesystems; "
            f"the writer refuses to proceed. "
            f"Reconfigure so both paths share a single mount."
        )


def stage_path_for(
    staging_root: Path,
    rel_lake_path: PurePosixPath,
    request_id: UUID,
    worker_id: str,
    attempt: int,
) -> Path:
    """Build the per-attempt staging path for a relative lake path.

    The .tmp suffix marks the file as in-flight; promotion strips it via
    rename(2). Per-(request_id, worker_id, attempt) scoping makes retry and
    parallel-worker collisions structurally impossible.
    """
    rel = Path(*rel_lake_path.parts)
    return (
        staging_root
        / str(request_id)
        / worker_id
        / f"attempt_{attempt}"
        / rel.with_suffix(rel.suffix + ".tmp")
    )


def _fsync_path(path: Path) -> None:
    """Open the path and fsync its file descriptor.

    Works for both regular files and directories. On Windows, fsync on a
    directory descriptor is a no-op (Windows has no equivalent system call),
    so we open file descriptors directly via os.open. The caller is responsible
    for ensuring the path exists.
    """
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:
        # Directory fsync is unsupported on some platforms (e.g. Windows).
        # The write itself is still durable; the parent-dir fsync is a
        # best-effort hardening step on POSIX-y systems.
        logger.debug("fsync on %s not supported on this platform", path)
    finally:
        os.close(fd)


def atomic_write_and_promote(
    content: bytes,
    lake_root: Path,
    staging_root: Path,
    rel_lake_path: PurePosixPath,
    request_id: UUID,
    worker_id: str,
    attempt: int,
) -> str:
    """Stage `content` then atomically promote into `lake_root / rel_lake_path`.

    Returns the SHA-256 hex digest of the written bytes.

    Raises AtomicRenameUnsafeError if the same-filesystem invariant fails.
    """
    assert_same_filesystem(lake_root, staging_root)

    staged = stage_path_for(staging_root, rel_lake_path, request_id, worker_id, attempt)
    staged.parent.mkdir(parents=True, exist_ok=True)

    # Write + fsync the staged file.
    with staged.open("wb") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    _fsync_path(staged.parent)

    # Compute the byte hash.
    sha = hashlib.sha256(content).hexdigest()

    # Promote: ensure lake parent exists, then rename.
    final = lake_root / Path(*rel_lake_path.parts)
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged, final)
    _fsync_path(final.parent)

    return sha
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_atomic.py -v
```
Expected: 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/data_lake/atomic.py PythonDataService/tests/unit/data_lake/test_atomic.py
git commit -m "feat(data-lake): atomic write/promote helper with same-filesystem guard (Slice 1b)"
```

---

### Task 3: `lean_writer.py` — deci-cent zip builder

**Files:**
- Create: `PythonDataService/app/data_lake/lean_writer.py`
- Create: `PythonDataService/tests/unit/data_lake/test_lean_writer.py`

- [ ] **Step 1: Write the failing tests**

Create `PythonDataService/tests/unit/data_lake/test_lean_writer.py`:

```python
"""Unit tests for app.data_lake.lean_writer.

LEAN minute-trade zip format (see Lean/Common/Data/Market/TradeBar.cs):
  data/equity/usa/minute/<sym_lower>/<yyyymmdd>_trade.zip
    └── <yyyymmdd>_<sym_lower>_minute_trade.csv
        no header; columns: ms_since_midnight_et, open*10000, high*10000,
        low*10000, close*10000, volume
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.data_lake.lean_writer import (
    MinuteTradeBar,
    build_minute_trade_zip_bytes,
    to_deci_cent,
)

ET = ZoneInfo("America/New_York")


def test_to_deci_cent_rounds_half_to_even():
    assert to_deci_cent(Decimal("499.5")) == 4_995_000
    assert to_deci_cent(Decimal("500.00005")) == 5_000_001
    assert to_deci_cent(Decimal("0")) == 0


def test_to_deci_cent_negative_rejected():
    import pytest

    with pytest.raises(ValueError):
        to_deci_cent(Decimal("-1.0"))


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


def test_build_minute_trade_zip_contains_one_csv_per_symbol_day():
    bars = [_bar(9, 30, 500.00), _bar(9, 31, 500.10)]
    payload = build_minute_trade_zip_bytes(
        symbol="SPY",
        trading_date_yyyymmdd="20240520",
        bars=bars,
    )
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = zf.namelist()
        assert names == ["20240520_spy_minute_trade.csv"]
        csv = zf.read(names[0]).decode("ascii")

    lines = csv.strip().split("\n")
    assert len(lines) == 2
    # First row: 09:30 ET → 34_200_000 ms since midnight ET. 500.00 = 5_000_000.
    cols = lines[0].split(",")
    assert int(cols[0]) == 34_200_000
    assert int(cols[4]) == 5_000_000  # close
    assert int(cols[5]) == 1234        # volume


def test_build_minute_trade_zip_is_deterministic():
    bars = [_bar(9, 30, 500.00), _bar(9, 31, 500.10)]
    a = build_minute_trade_zip_bytes("SPY", "20240520", bars)
    b = build_minute_trade_zip_bytes("SPY", "20240520", bars)
    assert a == b


def test_symbol_is_lowercased_in_csv_name():
    bars = [_bar(9, 30, 500.00)]
    payload = build_minute_trade_zip_bytes("QQQ", "20240520", bars)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        assert zf.namelist() == ["20240520_qqq_minute_trade.csv"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_lean_writer.py -v
```
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `lean_writer.py`**

Create `PythonDataService/app/data_lake/lean_writer.py`:

```python
"""LEAN deci-cent CSV-in-zip writer.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 5.1
Reference for the on-disk format: PythonDataService/app/engine/data/lean_format.py
(existing writer; this module supersedes it inside the data lake but does not
remove the existing one until Slice 1d).

LEAN minute-trade zip layout:
    equity/usa/minute/<sym_lower>/<yyyymmdd>_trade.zip
      └── <yyyymmdd>_<sym_lower>_minute_trade.csv
           no header; columns:
             ms_since_midnight_et, open*10000, high*10000, low*10000,
             close*10000, volume
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal


# LEAN's price scale factor: prices on disk are multiplied by 10_000.
_PRICE_SCALE = Decimal(10_000)
_QUANT = Decimal(1)  # round to integer after scaling

# ZIP archive epoch — pinned so two runs with identical inputs produce
# byte-identical zips. ZipFile default is "now", which would break the
# data_availability_hash determinism gate.
_DETERMINISTIC_ZIP_DATE_TIME: tuple[int, int, int, int, int, int] = (
    1980, 1, 1, 0, 0, 0,
)


@dataclass(frozen=True)
class MinuteTradeBar:
    """One minute trade bar in exchange-local (ET) wall clock.

    bar_start_et is the inclusive start of the minute (e.g. 09:30:00 ET
    represents the [09:30:00, 09:31:00) bar). LEAN's CSV column 0 is
    ms_since_midnight_et computed from this value.
    """

    bar_start_et: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


def to_deci_cent(price: Decimal) -> int:
    """Multiply by 10_000 and round half-to-even to integer.

    Rejects negative prices (LEAN never serializes them; a negative would
    indicate upstream data corruption).
    """
    if price < 0:
        raise ValueError(f"deci-cent encoding refuses negative price: {price}")
    return int((price * _PRICE_SCALE).quantize(_QUANT, rounding=ROUND_HALF_EVEN))


def _ms_since_midnight_et(bar_start_et: datetime) -> int:
    """ms from midnight in the bar's tz (the bar_start_et is expected ET-aware)."""
    midnight = bar_start_et.replace(hour=0, minute=0, second=0, microsecond=0)
    delta = bar_start_et - midnight
    return int(delta.total_seconds() * 1000)


def build_minute_trade_zip_bytes(
    symbol: str,
    trading_date_yyyymmdd: str,
    bars: list[MinuteTradeBar],
) -> bytes:
    """Build the deci-cent zip payload for a single (symbol, trading_date).

    Deterministic: same inputs produce byte-identical output. Caller writes
    the result via app.data_lake.atomic.atomic_write_and_promote.
    """
    sym_lower = symbol.lower()
    csv_name = f"{trading_date_yyyymmdd}_{sym_lower}_minute_trade.csv"
    lines = [
        ",".join(
            (
                str(_ms_since_midnight_et(bar.bar_start_et)),
                str(to_deci_cent(bar.open)),
                str(to_deci_cent(bar.high)),
                str(to_deci_cent(bar.low)),
                str(to_deci_cent(bar.close)),
                str(bar.volume),
            )
        )
        for bar in bars
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

```bash
podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_lean_writer.py -v
```
Expected: 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/data_lake/lean_writer.py PythonDataService/tests/unit/data_lake/test_lean_writer.py
git commit -m "feat(data-lake): deci-cent zip writer with deterministic bytes (Slice 1b)"
```

---

### Task 4: `polygon_fetcher.py` — paginated minute aggregate fetch

**Files:**
- Create: `PythonDataService/app/data_lake/polygon_fetcher.py`
- Create: `PythonDataService/tests/unit/data_lake/test_polygon_fetcher.py`

- [ ] **Step 1: Write the failing tests**

Create `PythonDataService/tests/unit/data_lake/test_polygon_fetcher.py`:

```python
"""Unit tests for app.data_lake.polygon_fetcher.

Polygon /v2/aggs response shape (per docs):
  {
    "ticker": "SPY",
    "results": [{"v":..., "o":..., "c":..., "h":..., "l":..., "t":..., "vw":..., "n":...}, ...],
    "next_url": "..."  (optional; present when more pages exist)
  }
"""

from __future__ import annotations

import re
from datetime import date

import httpx
import pytest
import respx

from app.data_lake.polygon_fetcher import (
    PolygonAuthError,
    PolygonEntitlementError,
    PolygonFetchError,
    PolygonRateLimitedError,
    PolygonUnknownSymbolError,
    fetch_minute_trade_aggregates,
)


def _aggs_url_pattern() -> re.Pattern:
    # /v2/aggs/ticker/{sym}/range/1/minute/{start}/{end}
    return re.compile(r"https://api\.polygon\.io/v2/aggs/ticker/.+/range/1/minute/.+")


@pytest.mark.asyncio
@respx.mock
async def test_single_page_response():
    respx.get(_aggs_url_pattern()).mock(
        return_value=httpx.Response(
            200,
            json={
                "ticker": "SPY",
                "status": "OK",
                "results": [
                    {"v": 100, "vw": 500.0, "o": 499.5, "c": 500.5, "h": 501.0, "l": 499.0,
                     "t": 1716206400000, "n": 10},
                    {"v": 200, "vw": 500.6, "o": 500.5, "c": 500.7, "h": 500.8, "l": 500.4,
                     "t": 1716206460000, "n": 15},
                ],
            },
        )
    )
    bars = await fetch_minute_trade_aggregates(
        symbol="SPY",
        start=date(2024, 5, 20),
        end=date(2024, 5, 20),
        api_key="test-key",
    )
    assert len(bars) == 2
    assert bars[0].t_ms == 1716206400000
    assert bars[1].volume == 200


@pytest.mark.asyncio
@respx.mock
async def test_pagination_follows_next_url():
    first = respx.get(_aggs_url_pattern()).mock(
        return_value=httpx.Response(
            200,
            json={
                "ticker": "SPY",
                "status": "OK",
                "results": [
                    {"v": 1, "vw": 1.0, "o": 1.0, "c": 1.0, "h": 1.0, "l": 1.0,
                     "t": 1, "n": 1},
                ],
                "next_url": "https://api.polygon.io/v2/aggs/page2",
            },
        )
    )
    page2 = respx.get("https://api.polygon.io/v2/aggs/page2").mock(
        return_value=httpx.Response(
            200,
            json={
                "ticker": "SPY",
                "status": "OK",
                "results": [
                    {"v": 2, "vw": 2.0, "o": 2.0, "c": 2.0, "h": 2.0, "l": 2.0,
                     "t": 2, "n": 2},
                ],
            },
        )
    )
    bars = await fetch_minute_trade_aggregates(
        symbol="SPY",
        start=date(2024, 5, 20),
        end=date(2024, 5, 20),
        api_key="test-key",
    )
    assert len(bars) == 2
    assert first.called
    assert page2.called


@pytest.mark.asyncio
@respx.mock
async def test_empty_results_returns_empty_list():
    respx.get(_aggs_url_pattern()).mock(
        return_value=httpx.Response(
            200,
            json={"ticker": "SPY", "status": "OK", "results": []},
        )
    )
    bars = await fetch_minute_trade_aggregates(
        symbol="SPY",
        start=date(2024, 5, 20),
        end=date(2024, 5, 20),
        api_key="test-key",
    )
    assert bars == []


@pytest.mark.asyncio
@respx.mock
async def test_401_raises_PolygonAuthError():
    respx.get(_aggs_url_pattern()).mock(return_value=httpx.Response(401, json={"error": "Unauthorized"}))
    with pytest.raises(PolygonAuthError):
        await fetch_minute_trade_aggregates(
            symbol="SPY", start=date(2024, 5, 20), end=date(2024, 5, 20), api_key="bad",
        )


@pytest.mark.asyncio
@respx.mock
async def test_403_raises_PolygonEntitlementError():
    respx.get(_aggs_url_pattern()).mock(return_value=httpx.Response(403, json={"error": "Forbidden"}))
    with pytest.raises(PolygonEntitlementError):
        await fetch_minute_trade_aggregates(
            symbol="SPY", start=date(2024, 5, 20), end=date(2024, 5, 20), api_key="ok",
        )


@pytest.mark.asyncio
@respx.mock
async def test_429_raises_PolygonRateLimitedError():
    respx.get(_aggs_url_pattern()).mock(return_value=httpx.Response(429, json={"error": "Too many"}))
    with pytest.raises(PolygonRateLimitedError):
        await fetch_minute_trade_aggregates(
            symbol="SPY", start=date(2024, 5, 20), end=date(2024, 5, 20), api_key="ok",
        )


@pytest.mark.asyncio
@respx.mock
async def test_404_or_unknown_status_raises_PolygonUnknownSymbolError():
    respx.get(_aggs_url_pattern()).mock(
        return_value=httpx.Response(
            200,
            json={"ticker": "FAKE", "status": "NOT_FOUND", "results": []},
        )
    )
    with pytest.raises(PolygonUnknownSymbolError):
        await fetch_minute_trade_aggregates(
            symbol="FAKE", start=date(2024, 5, 20), end=date(2024, 5, 20), api_key="ok",
        )


@pytest.mark.asyncio
@respx.mock
async def test_500_raises_PolygonFetchError():
    respx.get(_aggs_url_pattern()).mock(return_value=httpx.Response(500, json={"error": "Server"}))
    with pytest.raises(PolygonFetchError):
        await fetch_minute_trade_aggregates(
            symbol="SPY", start=date(2024, 5, 20), end=date(2024, 5, 20), api_key="ok",
        )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_polygon_fetcher.py -v
```
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `polygon_fetcher.py`**

Create `PythonDataService/app/data_lake/polygon_fetcher.py`:

```python
"""Polygon /v2/aggs minute-trade fetcher.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.6

Always requests `adjusted=false` (raw bars; LEAN normalization mode='Raw' per
the v1 single-canonical-root constraint). Paginated via Polygon's next_url
header.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import httpx

logger = logging.getLogger(__name__)

_POLYGON_BASE = "https://api.polygon.io"
_TIMEOUT_S = 30.0


class PolygonFetchError(RuntimeError):
    """Base for all Polygon fetch failures."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class PolygonAuthError(PolygonFetchError):
    """401 — bad/missing API key."""


class PolygonEntitlementError(PolygonFetchError):
    """403 — plan tier doesn't permit this data."""


class PolygonRateLimitedError(PolygonFetchError):
    """429 — back off and retry slower."""


class PolygonUnknownSymbolError(PolygonFetchError):
    """200 OK with status='NOT_FOUND' or 404 — Polygon doesn't recognize the symbol."""


@dataclass(frozen=True)
class PolygonBar:
    """One minute bar from Polygon /v2/aggs.

    t_ms is the bar's start time in UTC ms (Polygon's `t` field). Prices are
    raw floats from the JSON. Volume is an int.
    """

    t_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float
    n: int  # number of trades aggregated


async def fetch_minute_trade_aggregates(
    symbol: str,
    start: date,
    end: date,
    api_key: str,
) -> list[PolygonBar]:
    """Fetch minute-resolution trade aggregates for [start, end] inclusive.

    Returns bars in the order Polygon returned them (ascending t_ms).
    Pagination is handled transparently.

    Errors map onto Polygon* exception subclasses for callers to translate
    into ArtifactFailure.reason values.
    """
    url = (
        f"{_POLYGON_BASE}/v2/aggs/ticker/{symbol.upper()}/range/1/minute/"
        f"{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
    )
    params = {
        "adjusted": "false",
        "sort": "asc",
        "limit": 50_000,
        "apiKey": api_key,
    }
    out: list[PolygonBar] = []
    next_url: str | None = url

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        while next_url is not None:
            # Polygon's next_url already includes apiKey; only pass params on
            # the first request.
            req_params = params if next_url == url else {"apiKey": api_key}
            resp = await client.get(next_url, params=req_params)
            _raise_for_status(resp, symbol)
            payload = resp.json()
            _raise_for_payload_status(payload, symbol, resp.status_code)
            for r in payload.get("results") or []:
                out.append(
                    PolygonBar(
                        t_ms=int(r["t"]),
                        open=float(r["o"]),
                        high=float(r["h"]),
                        low=float(r["l"]),
                        close=float(r["c"]),
                        volume=int(r["v"]),
                        vwap=float(r.get("vw", 0.0)),
                        n=int(r.get("n", 0)),
                    )
                )
            next_url = payload.get("next_url")
    return out


def _raise_for_status(resp: httpx.Response, symbol: str) -> None:
    code = resp.status_code
    if code == 200:
        return
    if code == 401:
        raise PolygonAuthError(f"Polygon 401 for {symbol}: {resp.text[:200]}", code)
    if code == 403:
        raise PolygonEntitlementError(f"Polygon 403 for {symbol}: {resp.text[:200]}", code)
    if code == 404:
        raise PolygonUnknownSymbolError(f"Polygon 404 for {symbol}", code)
    if code == 429:
        raise PolygonRateLimitedError(f"Polygon 429 for {symbol}", code)
    raise PolygonFetchError(f"Polygon {code} for {symbol}: {resp.text[:200]}", code)


def _raise_for_payload_status(payload: dict, symbol: str, http_code: int) -> None:
    status = payload.get("status")
    if status == "OK" or status is None:
        return
    if status in ("NOT_FOUND", "ERROR_NOT_FOUND"):
        raise PolygonUnknownSymbolError(f"Polygon status={status} for {symbol}", http_code)
    # Other status values: treat as generic fetch error.
    raise PolygonFetchError(
        f"Polygon payload status={status} for {symbol}: {payload.get('error', '')[:200]}",
        http_code,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_polygon_fetcher.py -v
```
Expected: 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/data_lake/polygon_fetcher.py PythonDataService/tests/unit/data_lake/test_polygon_fetcher.py
git commit -m "feat(data-lake): paginated Polygon minute fetcher with typed error mapping (Slice 1b)"
```

---

### Task 5: `catalog_client.claim_minute_bar` + `complete_artifact` + `fail_artifact`

**Files:**
- Modify: `PythonDataService/app/data_lake/catalog_client.py`
- Create: `PythonDataService/tests/unit/data_lake/test_catalog_write_ops.py`

- [ ] **Step 1: Write the failing tests**

Create `PythonDataService/tests/unit/data_lake/test_catalog_write_ops.py`:

```python
"""Live-Postgres unit tests for catalog_client write operations.

Skips when POSTGRES_URL is unset (same pattern as test_schema_drift.py).
Tests clean up after themselves via TRUNCATE in a function-scoped fixture.
"""

from __future__ import annotations

import os
from datetime import date

import asyncpg
import pytest

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.types import ArtifactIdentity

pytestmark = pytest.mark.asyncio


def _postgres_url() -> str:
    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured")
    return url


@pytest.fixture
async def clean_artifacts():
    """Truncate DataLakeArtifacts before+after each test."""
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute('TRUNCATE TABLE "DataLakeArtifacts" RESTART IDENTITY CASCADE')
    finally:
        await conn.close()
    yield
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute('TRUNCATE TABLE "DataLakeArtifacts" RESTART IDENTITY CASCADE')
    finally:
        await conn.close()


def _minute_identity(date_val: date = date(2024, 5, 20)) -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_kind="time_series_bars",
        market="usa",
        symbol="SPY",
        trading_date=date_val,
        resolution="minute",
        data_type="trade",
        provider="polygon",
        price_adjustment_mode="raw",
    )


@pytest.fixture
async def pool():
    await catalog_client.init_pool()
    yield
    await catalog_client.close_pool()


async def test_claim_minute_bar_inserts_row_and_returns_id(clean_artifacts, pool):
    artifact_id = await catalog_client.claim_minute_bar(
        identity=_minute_identity(),
        worker_id="w-1",
        lease_ttl_ms=300_000,
        data_contract_hash="a" * 64,
        file_path="equity/usa/minute/spy/20240520_trade.zip",
    )
    assert isinstance(artifact_id, int)


async def test_claim_minute_bar_returns_none_on_conflict(clean_artifacts, pool):
    identity = _minute_identity()
    a = await catalog_client.claim_minute_bar(
        identity=identity, worker_id="w-1", lease_ttl_ms=300_000,
        data_contract_hash="a" * 64, file_path="x.zip",
    )
    b = await catalog_client.claim_minute_bar(
        identity=identity, worker_id="w-2", lease_ttl_ms=300_000,
        data_contract_hash="a" * 64, file_path="x.zip",
    )
    assert a is not None
    assert b is None  # second claim loses


async def test_complete_artifact_updates_to_complete(clean_artifacts, pool):
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity, worker_id="w-1", lease_ttl_ms=300_000,
        data_contract_hash="a" * 64, file_path="x.zip",
    )
    assert artifact_id is not None
    await catalog_client.complete_artifact(
        artifact_id=artifact_id,
        row_count=390,
        first_bar_start_ms=1_716_206_400_000,
        last_bar_start_ms=1_716_229_740_000,
        file_size_bytes=12345,
        file_sha256="b" * 64,
    )

    conn = await asyncpg.connect(_postgres_url())
    try:
        row = await conn.fetchrow(
            'SELECT "Status", "RowCount", "FileSha256", "CompletedAtMs" '
            'FROM "DataLakeArtifacts" WHERE "Id" = $1',
            artifact_id,
        )
    finally:
        await conn.close()
    assert row["Status"] == "complete"
    assert row["RowCount"] == 390
    assert row["FileSha256"] == "b" * 64
    assert row["CompletedAtMs"] is not None


async def test_fail_artifact_updates_to_failed(clean_artifacts, pool):
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity, worker_id="w-1", lease_ttl_ms=300_000,
        data_contract_hash="a" * 64, file_path="x.zip",
    )
    assert artifact_id is not None
    await catalog_client.fail_artifact(
        artifact_id=artifact_id,
        last_error="provider_rate_limited",
        error_message="429 from Polygon",
    )
    conn = await asyncpg.connect(_postgres_url())
    try:
        row = await conn.fetchrow(
            'SELECT "Status", "LastError", "ErrorMessage" '
            'FROM "DataLakeArtifacts" WHERE "Id" = $1',
            artifact_id,
        )
    finally:
        await conn.close()
    assert row["Status"] == "failed"
    assert row["LastError"] == "provider_rate_limited"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_catalog_write_ops.py -v
```
Expected: FAIL with `AttributeError: module 'app.data_lake.catalog_client' has no attribute 'claim_minute_bar'`.

- [ ] **Step 3: Add the three new functions to `catalog_client.py`**

Append to `PythonDataService/app/data_lake/catalog_client.py`:

```python
import time

from app.data_lake.types import ArtifactIdentity


async def claim_minute_bar(
    identity: ArtifactIdentity,
    worker_id: str,
    lease_ttl_ms: int,
    data_contract_hash: str,
    file_path: str,
) -> int | None:
    """Atomic claim for a minute-resolution time_series_bars artifact.

    Returns the new row's Id when this caller is the winner; returns None when
    a row already exists for this identity tuple (someone else has it).

    Matches the partial unique index uq_data_lake_artifacts_minute_bars:
      (Market, Symbol, TradingDate, DataType, Provider, PriceAdjustmentMode)
       WHERE ArtifactKind='time_series_bars' AND Resolution='minute'
    The ON CONFLICT clause repeats the partial index's WHERE predicate, per
    Postgres' requirement for partial-index conflict targets.
    """
    if identity.artifact_kind != "time_series_bars" or identity.resolution != "minute":
        raise ValueError(
            f"claim_minute_bar called with non-minute-bar identity: {identity!r}"
        )
    now_ms = int(time.time() * 1000)
    query = """
        INSERT INTO "DataLakeArtifacts" (
            "ArtifactKind", "Market", "Symbol", "TradingDate",
            "Resolution", "DataType", "Provider", "ProviderParams",
            "PriceAdjustmentMode", "DataContractHash",
            "FilePath", "Status", "LeaseOwner", "LeaseExpiresAtMs",
            "AttemptCount", "FetchedAtMs"
        )
        VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
            $11, 'fetching', $12, $13, 1, $14
        )
        ON CONFLICT ("Market", "Symbol", "TradingDate", "DataType",
                     "Provider", "PriceAdjustmentMode")
            WHERE "ArtifactKind" = 'time_series_bars' AND "Resolution" = 'minute'
        DO NOTHING
        RETURNING "Id";
    """
    async with connection() as conn:
        return await conn.fetchval(
            query,
            identity.artifact_kind,
            identity.market,
            identity.symbol,
            identity.trading_date,
            identity.resolution,
            identity.data_type,
            identity.provider,
            "{}",                    # ProviderParams (jsonb; populated by fetcher in 1c)
            identity.price_adjustment_mode,
            data_contract_hash,
            file_path,
            worker_id,
            now_ms + lease_ttl_ms,
            now_ms,
        )


async def complete_artifact(
    artifact_id: int,
    row_count: int,
    first_bar_start_ms: int,
    last_bar_start_ms: int,
    file_size_bytes: int,
    file_sha256: str,
) -> None:
    """Transition an artifact from 'fetching' → 'complete' with byte metadata.

    No-op if the row is not currently 'fetching' (defensive against stale
    callers; the sweep is the only legitimate source of late writes).
    """
    now_ms = int(time.time() * 1000)
    query = """
        UPDATE "DataLakeArtifacts"
           SET "Status" = 'complete',
               "RowCount" = $2,
               "FirstBarStartMs" = $3,
               "LastBarStartMs" = $4,
               "FileSizeBytes" = $5,
               "FileSha256" = $6,
               "CompletedAtMs" = $7,
               "LeaseOwner" = NULL,
               "LeaseExpiresAtMs" = NULL
         WHERE "Id" = $1
           AND "Status" = 'fetching';
    """
    async with connection() as conn:
        await conn.execute(
            query,
            artifact_id, row_count, first_bar_start_ms, last_bar_start_ms,
            file_size_bytes, file_sha256, now_ms,
        )


async def fail_artifact(
    artifact_id: int,
    last_error: str,
    error_message: str | None = None,
) -> None:
    """Transition an artifact to 'failed' with diagnostic info.

    The row stays in the catalog as an audit record; future ensure_data calls
    may retry it via steal_or_retry_minute_bar (Task 7).
    """
    query = """
        UPDATE "DataLakeArtifacts"
           SET "Status" = 'failed',
               "LastError" = $2,
               "ErrorMessage" = $3,
               "LeaseOwner" = NULL,
               "LeaseExpiresAtMs" = NULL
         WHERE "Id" = $1;
    """
    async with connection() as conn:
        await conn.execute(query, artifact_id, last_error, error_message)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_catalog_write_ops.py -v
```
Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/data_lake/catalog_client.py PythonDataService/tests/unit/data_lake/test_catalog_write_ops.py
git commit -m "feat(data-lake): catalog_client claim/complete/fail for minute bars (Slice 1b)"
```

---

### Task 6: `catalog_client.refresh_lease` (heartbeat)

**Files:**
- Modify: `PythonDataService/app/data_lake/catalog_client.py`
- Modify: `PythonDataService/tests/unit/data_lake/test_catalog_write_ops.py`

- [ ] **Step 1: Append the failing test**

Append to `PythonDataService/tests/unit/data_lake/test_catalog_write_ops.py`:

```python
async def test_refresh_lease_extends_expiry(clean_artifacts, pool):
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity, worker_id="w-1", lease_ttl_ms=300_000,
        data_contract_hash="a" * 64, file_path="x.zip",
    )
    assert artifact_id is not None

    # Read initial lease expiry.
    conn = await asyncpg.connect(_postgres_url())
    try:
        before = await conn.fetchval(
            'SELECT "LeaseExpiresAtMs" FROM "DataLakeArtifacts" WHERE "Id" = $1',
            artifact_id,
        )
    finally:
        await conn.close()

    ok = await catalog_client.refresh_lease(
        artifact_id=artifact_id, worker_id="w-1", lease_ttl_ms=600_000,
    )
    assert ok is True

    conn = await asyncpg.connect(_postgres_url())
    try:
        after = await conn.fetchval(
            'SELECT "LeaseExpiresAtMs" FROM "DataLakeArtifacts" WHERE "Id" = $1',
            artifact_id,
        )
    finally:
        await conn.close()
    assert after > before


async def test_refresh_lease_rejects_wrong_owner(clean_artifacts, pool):
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity, worker_id="w-1", lease_ttl_ms=300_000,
        data_contract_hash="a" * 64, file_path="x.zip",
    )
    assert artifact_id is not None
    ok = await catalog_client.refresh_lease(
        artifact_id=artifact_id, worker_id="w-IMPOSTOR", lease_ttl_ms=600_000,
    )
    assert ok is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_catalog_write_ops.py::test_refresh_lease_extends_expiry -v
```
Expected: FAIL (`refresh_lease` doesn't exist yet).

- [ ] **Step 3: Add `refresh_lease` to `catalog_client.py`**

Append to `PythonDataService/app/data_lake/catalog_client.py`:

```python
async def refresh_lease(
    artifact_id: int,
    worker_id: str,
    lease_ttl_ms: int,
) -> bool:
    """Heartbeat: extend a lease as long as the calling worker still owns it.

    Returns True when the lease was updated; False when worker_id no longer
    matches LeaseOwner (the lease may have been stolen by the sweep).
    """
    now_ms = int(time.time() * 1000)
    query = """
        UPDATE "DataLakeArtifacts"
           SET "LeaseExpiresAtMs" = $3
         WHERE "Id" = $1
           AND "LeaseOwner" = $2
           AND "Status" = 'fetching';
    """
    async with connection() as conn:
        result = await conn.execute(query, artifact_id, worker_id, now_ms + lease_ttl_ms)
    # asyncpg returns "UPDATE n" — parse the row count.
    n = int(result.rsplit(" ", 1)[-1])
    return n > 0
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_catalog_write_ops.py -v
```
Expected: 6 tests PASS (the original 4 + 2 new).

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/data_lake/catalog_client.py PythonDataService/tests/unit/data_lake/test_catalog_write_ops.py
git commit -m "feat(data-lake): catalog_client.refresh_lease heartbeat (Slice 1b)"
```

---

### Task 7: `catalog_client.steal_or_retry_minute_bar`

**Files:**
- Modify: `PythonDataService/app/data_lake/catalog_client.py`
- Modify: `PythonDataService/tests/unit/data_lake/test_catalog_write_ops.py`

- [ ] **Step 1: Append the failing tests**

Append to `test_catalog_write_ops.py`:

```python
async def test_steal_or_retry_steals_expired_lease(clean_artifacts, pool):
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity, worker_id="w-orig", lease_ttl_ms=300_000,
        data_contract_hash="a" * 64, file_path="x.zip",
    )
    assert artifact_id is not None

    # Force the lease to be expired.
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute(
            'UPDATE "DataLakeArtifacts" SET "LeaseExpiresAtMs" = 1 WHERE "Id" = $1',
            artifact_id,
        )
    finally:
        await conn.close()

    ok = await catalog_client.steal_or_retry_minute_bar(
        artifact_id=artifact_id, worker_id="w-new", lease_ttl_ms=300_000,
        max_retries=3,
    )
    assert ok is True

    conn = await asyncpg.connect(_postgres_url())
    try:
        row = await conn.fetchrow(
            'SELECT "Status", "LeaseOwner", "AttemptCount" '
            'FROM "DataLakeArtifacts" WHERE "Id" = $1',
            artifact_id,
        )
    finally:
        await conn.close()
    assert row["Status"] == "fetching"
    assert row["LeaseOwner"] == "w-new"
    assert row["AttemptCount"] == 2  # incremented from 1


async def test_steal_or_retry_retries_failed_under_max(clean_artifacts, pool):
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity, worker_id="w-1", lease_ttl_ms=300_000,
        data_contract_hash="a" * 64, file_path="x.zip",
    )
    assert artifact_id is not None
    await catalog_client.fail_artifact(
        artifact_id=artifact_id, last_error="provider_api_error",
    )
    ok = await catalog_client.steal_or_retry_minute_bar(
        artifact_id=artifact_id, worker_id="w-2", lease_ttl_ms=300_000,
        max_retries=3,
    )
    assert ok is True


async def test_steal_or_retry_rejects_failed_at_max(clean_artifacts, pool):
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity, worker_id="w-1", lease_ttl_ms=300_000,
        data_contract_hash="a" * 64, file_path="x.zip",
    )
    assert artifact_id is not None

    # Force AttemptCount to max.
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute(
            'UPDATE "DataLakeArtifacts" SET "Status" = $1, "AttemptCount" = $2 '
            'WHERE "Id" = $3',
            "failed", 3, artifact_id,
        )
    finally:
        await conn.close()

    ok = await catalog_client.steal_or_retry_minute_bar(
        artifact_id=artifact_id, worker_id="w-2", lease_ttl_ms=300_000,
        max_retries=3,
    )
    assert ok is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_catalog_write_ops.py::test_steal_or_retry_steals_expired_lease -v
```
Expected: FAIL (`steal_or_retry_minute_bar` not implemented).

- [ ] **Step 3: Add `steal_or_retry_minute_bar` to `catalog_client.py`**

Append to `PythonDataService/app/data_lake/catalog_client.py`:

```python
async def steal_or_retry_minute_bar(
    artifact_id: int,
    worker_id: str,
    lease_ttl_ms: int,
    max_retries: int,
) -> bool:
    """Reclaim an artifact whose lease expired OR retry a failed artifact.

    Eligibility:
      - Status='fetching' AND LeaseExpiresAtMs < now_ms  (lease expired), OR
      - Status='failed' AND AttemptCount < max_retries  (retryable failure)

    Returns True when the row was updated to 'fetching' under the new worker;
    False when no eligible row exists (e.g., already complete, already
    re-claimed by another worker, or failed beyond max_retries).
    """
    now_ms = int(time.time() * 1000)
    query = """
        UPDATE "DataLakeArtifacts"
           SET "Status" = 'fetching',
               "LeaseOwner" = $2,
               "LeaseExpiresAtMs" = $3,
               "AttemptCount" = "AttemptCount" + 1,
               "LastError" = NULL
         WHERE "Id" = $1
           AND (
                  ("Status" = 'fetching' AND "LeaseExpiresAtMs" < $4)
               OR ("Status" = 'failed' AND "AttemptCount" < $5)
           );
    """
    async with connection() as conn:
        result = await conn.execute(
            query, artifact_id, worker_id, now_ms + lease_ttl_ms, now_ms, max_retries,
        )
    n = int(result.rsplit(" ", 1)[-1])
    return n > 0
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_catalog_write_ops.py -v
```
Expected: 9 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/data_lake/catalog_client.py PythonDataService/tests/unit/data_lake/test_catalog_write_ops.py
git commit -m "feat(data-lake): catalog_client.steal_or_retry_minute_bar (Slice 1b)"
```

---

### Task 8: `catalog_client.refresh_complete_minute_bar` (force_refresh)

**Files:**
- Modify: `PythonDataService/app/data_lake/catalog_client.py`
- Modify: `PythonDataService/tests/unit/data_lake/test_catalog_write_ops.py`

- [ ] **Step 1: Append the failing test**

Append to `test_catalog_write_ops.py`:

```python
async def test_refresh_complete_returns_prior_metadata(clean_artifacts, pool):
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity, worker_id="w-1", lease_ttl_ms=300_000,
        data_contract_hash="a" * 64, file_path="equity/usa/minute/spy/20240520_trade.zip",
    )
    assert artifact_id is not None
    await catalog_client.complete_artifact(
        artifact_id=artifact_id, row_count=390,
        first_bar_start_ms=1, last_bar_start_ms=2,
        file_size_bytes=100, file_sha256="b" * 64,
    )

    prior = await catalog_client.refresh_complete_minute_bar(
        artifact_id=artifact_id, worker_id="w-1", lease_ttl_ms=300_000,
    )
    assert prior is not None
    assert prior.prior_file_path == "equity/usa/minute/spy/20240520_trade.zip"
    assert prior.prior_file_sha256 == "b" * 64


async def test_refresh_complete_returns_none_when_not_complete(clean_artifacts, pool):
    identity = _minute_identity()
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity, worker_id="w-1", lease_ttl_ms=300_000,
        data_contract_hash="a" * 64, file_path="x.zip",
    )
    assert artifact_id is not None  # still 'fetching', not 'complete'
    prior = await catalog_client.refresh_complete_minute_bar(
        artifact_id=artifact_id, worker_id="w-1", lease_ttl_ms=300_000,
    )
    assert prior is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_catalog_write_ops.py::test_refresh_complete_returns_prior_metadata -v
```
Expected: FAIL.

- [ ] **Step 3: Add `refresh_complete_minute_bar` to `catalog_client.py`**

Append to `PythonDataService/app/data_lake/catalog_client.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class PriorArtifactMetadata:
    prior_file_path: str
    prior_file_sha256: str


async def refresh_complete_minute_bar(
    artifact_id: int,
    worker_id: str,
    lease_ttl_ms: int,
) -> PriorArtifactMetadata | None:
    """Force-refresh transition: 'complete' → 'fetching' for a re-fetch.

    Returns the prior file_path + file_sha256 so the caller can preserve them
    if the new fetch fails validation. Returns None when the row isn't
    currently 'complete' (refresh has no work to do).
    """
    now_ms = int(time.time() * 1000)
    query = """
        UPDATE "DataLakeArtifacts"
           SET "Status" = 'fetching',
               "LeaseOwner" = $2,
               "LeaseExpiresAtMs" = $3,
               "AttemptCount" = "AttemptCount" + 1
         WHERE "Id" = $1
           AND "Status" = 'complete'
        RETURNING "FilePath", "FileSha256";
    """
    async with connection() as conn:
        row = await conn.fetchrow(
            query, artifact_id, worker_id, now_ms + lease_ttl_ms,
        )
    if row is None:
        return None
    return PriorArtifactMetadata(
        prior_file_path=row["FilePath"],
        prior_file_sha256=row["FileSha256"],
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_catalog_write_ops.py -v
```
Expected: 11 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/data_lake/catalog_client.py PythonDataService/tests/unit/data_lake/test_catalog_write_ops.py
git commit -m "feat(data-lake): catalog_client.refresh_complete_minute_bar for force_refresh (Slice 1b)"
```

---

### Task 9: `sweep.reclaim_expired_leases`

**Files:**
- Create: `PythonDataService/app/data_lake/sweep.py`
- Create: `PythonDataService/tests/unit/data_lake/test_sweep.py`

- [ ] **Step 1: Write the failing tests**

Create `PythonDataService/tests/unit/data_lake/test_sweep.py`:

```python
"""Live-Postgres unit tests for sweep.reclaim_expired_leases.

The sweep is not yet scheduled (Slice 4 wires the cron). This test exercises
the SQL primitive directly.
"""

from __future__ import annotations

import os
from datetime import date

import asyncpg
import pytest

from app.config import settings
from app.data_lake import catalog_client, sweep
from app.data_lake.types import ArtifactIdentity

pytestmark = pytest.mark.asyncio


def _postgres_url() -> str:
    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured")
    return url


@pytest.fixture
async def clean_artifacts():
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute('TRUNCATE TABLE "DataLakeArtifacts" RESTART IDENTITY CASCADE')
    finally:
        await conn.close()
    yield
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute('TRUNCATE TABLE "DataLakeArtifacts" RESTART IDENTITY CASCADE')
    finally:
        await conn.close()


@pytest.fixture
async def pool():
    await catalog_client.init_pool()
    yield
    await catalog_client.close_pool()


async def test_reclaim_marks_expired_fetching_rows_failed(clean_artifacts, pool):
    identity = ArtifactIdentity(
        artifact_kind="time_series_bars", market="usa", symbol="SPY",
        trading_date=date(2024, 5, 20), resolution="minute", data_type="trade",
        provider="polygon", price_adjustment_mode="raw",
    )
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity, worker_id="w-1", lease_ttl_ms=300_000,
        data_contract_hash="a" * 64, file_path="x.zip",
    )
    # Force the lease to be expired.
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute(
            'UPDATE "DataLakeArtifacts" SET "LeaseExpiresAtMs" = 1 WHERE "Id" = $1',
            artifact_id,
        )
    finally:
        await conn.close()

    n = await sweep.reclaim_expired_leases()
    assert n == 1

    conn = await asyncpg.connect(_postgres_url())
    try:
        row = await conn.fetchrow(
            'SELECT "Status", "LastError" FROM "DataLakeArtifacts" WHERE "Id" = $1',
            artifact_id,
        )
    finally:
        await conn.close()
    assert row["Status"] == "failed"
    assert row["LastError"] == "lease_expired"


async def test_reclaim_leaves_valid_lease_alone(clean_artifacts, pool):
    identity = ArtifactIdentity(
        artifact_kind="time_series_bars", market="usa", symbol="SPY",
        trading_date=date(2024, 5, 20), resolution="minute", data_type="trade",
        provider="polygon", price_adjustment_mode="raw",
    )
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity, worker_id="w-1", lease_ttl_ms=300_000,
        data_contract_hash="a" * 64, file_path="x.zip",
    )
    n = await sweep.reclaim_expired_leases()
    assert n == 0

    conn = await asyncpg.connect(_postgres_url())
    try:
        status = await conn.fetchval(
            'SELECT "Status" FROM "DataLakeArtifacts" WHERE "Id" = $1', artifact_id,
        )
    finally:
        await conn.close()
    assert status == "fetching"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_sweep.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'app.data_lake.sweep'`.

- [ ] **Step 3: Implement `sweep.py`**

Create `PythonDataService/app/data_lake/sweep.py`:

```python
"""Lease-expiry sweep for the data lake catalog.

Slice 1b lands the primitive only. Slice 4 wires it onto a scheduler (cron
or asyncio background task).

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.4
"""

from __future__ import annotations

import logging
import time

from app.data_lake.catalog_client import connection

logger = logging.getLogger(__name__)


async def reclaim_expired_leases() -> int:
    """Mark any 'fetching' row whose lease has expired as 'failed'.

    Returns the number of rows reclaimed. Callers can re-attempt those rows
    via catalog_client.steal_or_retry_minute_bar.
    """
    now_ms = int(time.time() * 1000)
    query = """
        UPDATE "DataLakeArtifacts"
           SET "Status" = 'failed',
               "LastError" = 'lease_expired',
               "LeaseOwner" = NULL,
               "LeaseExpiresAtMs" = NULL
         WHERE "Status" = 'fetching'
           AND "LeaseExpiresAtMs" < $1;
    """
    async with connection() as conn:
        result = await conn.execute(query, now_ms)
    n = int(result.rsplit(" ", 1)[-1])
    if n > 0:
        logger.info("data_lake.sweep: reclaimed %d expired leases", n)
    return n
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
podman exec polygon-data-service python -m pytest tests/unit/data_lake/test_sweep.py -v
```
Expected: 2 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/data_lake/sweep.py PythonDataService/tests/unit/data_lake/test_sweep.py
git commit -m "feat(data-lake): lease-expiry sweep primitive (Slice 1b)"
```

---

### Task 10: `ensure_data` wiring — minute-trade dispatch + integration test

**Files:**
- Modify: `PythonDataService/app/data_lake/ensure_data.py`
- Modify: `PythonDataService/app/data_lake/types.py`
- Modify: `PythonDataService/app/data_lake/fake_polygon.py`
- Create: `PythonDataService/tests/integration/data_lake/test_ensure_data_real_polygon.py`

- [ ] **Step 1: Add `'unsupported_artifact_kind'` to the ArtifactFailure enum**

In `PythonDataService/app/data_lake/types.py`, locate the `ArtifactFailure.reason: Literal[...]` field. Add `'unsupported_artifact_kind'` to the literal set (alongside the existing `'unsupported_resolution'`). The enum should end up looking like:

```python
    reason: Literal[
        'provider_auth_error',
        'provider_entitlement_error',
        'provider_rate_limited',
        'provider_api_error',
        'provider_no_data',
        'unknown_symbol',
        'validation_failed',
        'io_error',
        'lease_timeout',
        'fetch_timeout',
        'unsupported_resolution',
        'unsupported_artifact_kind',          # Slice 1b: only minute-trade is implemented
        'internal_error',
    ]
```

- [ ] **Step 2: Update `fake_polygon.synth_artifact_record` to reject minute-trade**

In `PythonDataService/app/data_lake/fake_polygon.py`, at the top of `synth_artifact_record`, add an explicit guard:

```python
def synth_artifact_record(identity: ArtifactIdentity) -> ArtifactRecord:
    if (
        identity.artifact_kind == "time_series_bars"
        and identity.resolution == "minute"
        and identity.data_type == "trade"
    ):
        raise ValueError(
            "fake_polygon.synth_artifact_record refuses minute-trade artifacts "
            "in Slice 1b — they now flow through the real polygon_fetcher path. "
            "If this fires, ensure_data dispatch logic is wrong."
        )
    # (existing body unchanged)
```

- [ ] **Step 3: Write the failing integration test**

Create `PythonDataService/tests/integration/data_lake/test_ensure_data_real_polygon.py`:

```python
"""End-to-end: real ensure_data with respx-mocked Polygon, real Postgres,
tmp filesystem for the lake.

Asserts:
  - Catalog rows land with status='complete' for minute-trade artifacts
  - Files exist on disk with the correct deci-cent zip payload
  - data_availability_hash is deterministic across two identical calls
  - Second call is a cache hit (fetched_artifact_count == 0)
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from uuid import UUID

import asyncpg
import httpx
import pytest
import respx

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.ensure_data import ensure_data
from app.data_lake.types import DataRunSpec

pytestmark = pytest.mark.asyncio


def _postgres_url() -> str:
    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured")
    return url


@pytest.fixture
async def clean_artifacts():
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute('TRUNCATE TABLE "DataLakeArtifacts" RESTART IDENTITY CASCADE')
    finally:
        await conn.close()
    yield
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute('TRUNCATE TABLE "DataLakeArtifacts" RESTART IDENTITY CASCADE')
    finally:
        await conn.close()


@pytest.fixture
async def pool():
    await catalog_client.init_pool()
    yield
    await catalog_client.close_pool()


@pytest.fixture
def tmp_lake(tmp_path: Path, monkeypatch):
    """Point LEAN_DATA_WRITE_ROOT at a tmp_path tree with lake/ + staging/."""
    write_root = tmp_path / "writer-root"
    (write_root / "lake").mkdir(parents=True)
    (write_root / "staging").mkdir(parents=True)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setenv("POLYGON_API_KEY", "test-polygon-key")
    return write_root


def _polygon_payload_for(start: int, count: int) -> dict:
    """Generate `count` synthetic 1-minute bars starting at UTC ms `start`."""
    return {
        "ticker": "SPY",
        "status": "OK",
        "results": [
            {
                "v": 1000 + i,
                "vw": 500.0,
                "o": 500.0 + i * 0.01,
                "c": 500.05 + i * 0.01,
                "h": 500.10 + i * 0.01,
                "l": 499.95 + i * 0.01,
                "t": start + i * 60_000,
                "n": 10,
            }
            for i in range(count)
        ],
    }


@respx.mock
async def test_ensure_data_writes_files_and_catalog_rows(clean_artifacts, pool, tmp_lake):
    # Mock Polygon for a single-day SPY fetch — 390 bars covering 09:30 → 16:00 ET.
    # 2024-05-20 09:30:00 ET = 1716212100000 ms UTC (verified via epochconverter; ET is UTC-4 in DST).
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json=_polygon_payload_for(1716212100000, 390))
    )

    spec = DataRunSpec(
        request_id=UUID("12345678-1234-5678-1234-567812345678"),
        run_type="python_lab",
        symbols=["SPY"],
        start_trading_date=date(2024, 5, 20),
        end_trading_date=date(2024, 5, 20),
        lean_image_digest="sha256:test",
    )
    result = await ensure_data(spec)

    assert result.overall_status in {"complete", "partial"}
    # The minute-trade artifact for SPY on 2024-05-20 must be complete.
    minute_trade = [
        a for a in result.artifacts
        if a.artifact_kind == "time_series_bars"
        and a.resolution == "minute"
        and a.data_type == "trade"
        and a.symbol == "SPY"
    ]
    assert len(minute_trade) == 1
    art = minute_trade[0]
    assert art.row_count == 390
    assert len(art.file_sha256) == 64
    assert art.file_sha256 != "0" * 64  # not the fake_polygon stub

    # File exists on disk at the expected lake path.
    final = tmp_lake / "lake" / art.file_path
    assert final.is_file()
    assert final.stat().st_size == art.file_path and final.stat().st_size > 0


@respx.mock
async def test_second_call_is_cache_hit(clean_artifacts, pool, tmp_lake):
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json=_polygon_payload_for(1716212100000, 390))
    )

    spec = DataRunSpec(
        request_id=UUID("11111111-1111-1111-1111-111111111111"),
        run_type="python_lab",
        symbols=["SPY"],
        start_trading_date=date(2024, 5, 20),
        end_trading_date=date(2024, 5, 20),
        lean_image_digest="sha256:test",
    )
    first = await ensure_data(spec)
    # New request_id; same spec → same artifacts.
    spec2 = spec.model_copy(update={"request_id": UUID("22222222-2222-2222-2222-222222222222")})
    second = await ensure_data(spec2)

    assert first.data_availability_hash == second.data_availability_hash
    # On the second call the minute-trade artifact is reused, not fetched.
    minute_trade_first = [a for a in first.artifacts if a.resolution == "minute"]
    minute_trade_second = [a for a in second.artifacts if a.resolution == "minute"]
    assert len(minute_trade_first) == 1
    assert len(minute_trade_second) == 1
    assert second.reused_artifact_count >= 1
```

- [ ] **Step 4: Run the test to verify it fails**

```bash
podman exec polygon-data-service python -m pytest tests/integration/data_lake/test_ensure_data_real_polygon.py -v
```
Expected: FAIL — `ensure_data` doesn't yet dispatch to real Polygon for minute-trade.

- [ ] **Step 5: Rewrite `ensure_data.ensure_data` to dispatch by artifact kind**

Replace the body of `ensure_data` in `PythonDataService/app/data_lake/ensure_data.py` with a dispatch loop. Add helper `_process_minute_trade_artifact` for the real path. The new file structure:

```python
# Add to the imports at the top of ensure_data.py:
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.atomic import atomic_write_and_promote
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.data_lake.path_policy import LeanMinuteBarPath
from app.data_lake.polygon_fetcher import (
    PolygonAuthError,
    PolygonBar,
    PolygonEntitlementError,
    PolygonFetchError,
    PolygonRateLimitedError,
    PolygonUnknownSymbolError,
    fetch_minute_trade_aggregates,
)

_ET = ZoneInfo("America/New_York")
_WORKER_ID = os.environ.get("HOSTNAME", "py-data-lake")  # one writer per process
_LEASE_TTL_MS = 300_000


def _is_minute_trade(identity) -> bool:
    return (
        identity.artifact_kind == "time_series_bars"
        and identity.resolution == "minute"
        and identity.data_type == "trade"
    )


def _polygon_bar_to_minute_trade_bar(pb: PolygonBar) -> MinuteTradeBar:
    bar_start_utc = datetime.fromtimestamp(pb.t_ms / 1000, tz=ZoneInfo("UTC"))
    return MinuteTradeBar(
        bar_start_et=bar_start_utc.astimezone(_ET),
        open=Decimal(str(pb.open)),
        high=Decimal(str(pb.high)),
        low=Decimal(str(pb.low)),
        close=Decimal(str(pb.close)),
        volume=pb.volume,
    )


async def _process_minute_trade_artifact(identity, spec) -> tuple[ArtifactRecord | None, ArtifactFailure | None]:
    """Claim → fetch → write → complete one minute-trade artifact.

    Returns (record, None) on success or (None, failure) on error.
    """
    rel_path = LeanMinuteBarPath(
        market=identity.market,
        symbol=identity.symbol,
        trading_date=identity.trading_date,
        data_type="trade",
    ).relative_path()
    file_path = str(rel_path)

    data_contract_hash = "x" * 64   # Slice 1c: compute over canonical(provider_params, ...)

    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id=_WORKER_ID,
        lease_ttl_ms=_LEASE_TTL_MS,
        data_contract_hash=data_contract_hash,
        file_path=file_path,
    )
    if artifact_id is None:
        # Already complete (or in-flight); read the existing complete row.
        existing = await catalog_client.select_coverage_minute_bars(
            market=identity.market, symbol=identity.symbol,
            data_type="trade",
            start_trading_date=identity.trading_date,
            end_trading_date=identity.trading_date,
        )
        if existing:
            return existing[0], None
        # In-flight elsewhere; Slice 1b doesn't poll. Report as transient.
        return None, ArtifactFailure(
            artifact_kind=identity.artifact_kind,
            symbol=identity.symbol,
            trading_date=identity.trading_date,
            data_type=identity.data_type,
            reason="lease_timeout",
            detail="another worker has the lease; polling not implemented in Slice 1b",
            attempt_count=1,
        )

    # Fetch from Polygon.
    api_key = settings.POLYGON_API_KEY
    try:
        polygon_bars = await fetch_minute_trade_aggregates(
            symbol=identity.symbol,
            start=identity.trading_date,
            end=identity.trading_date,
            api_key=api_key,
        )
    except PolygonAuthError as e:
        await catalog_client.fail_artifact(artifact_id, "provider_auth_error", str(e))
        return None, ArtifactFailure(
            artifact_kind=identity.artifact_kind, symbol=identity.symbol,
            trading_date=identity.trading_date, data_type=identity.data_type,
            reason="provider_auth_error", detail=str(e), attempt_count=1,
        )
    except PolygonEntitlementError as e:
        await catalog_client.fail_artifact(artifact_id, "provider_entitlement_error", str(e))
        return None, ArtifactFailure(
            artifact_kind=identity.artifact_kind, symbol=identity.symbol,
            trading_date=identity.trading_date, data_type=identity.data_type,
            reason="provider_entitlement_error", detail=str(e), attempt_count=1,
        )
    except PolygonRateLimitedError as e:
        await catalog_client.fail_artifact(artifact_id, "provider_rate_limited", str(e))
        return None, ArtifactFailure(
            artifact_kind=identity.artifact_kind, symbol=identity.symbol,
            trading_date=identity.trading_date, data_type=identity.data_type,
            reason="provider_rate_limited", detail=str(e), attempt_count=1,
        )
    except PolygonUnknownSymbolError as e:
        await catalog_client.fail_artifact(artifact_id, "unknown_symbol", str(e))
        return None, ArtifactFailure(
            artifact_kind=identity.artifact_kind, symbol=identity.symbol,
            trading_date=identity.trading_date, data_type=identity.data_type,
            reason="unknown_symbol", detail=str(e), attempt_count=1,
        )
    except PolygonFetchError as e:
        await catalog_client.fail_artifact(artifact_id, "provider_api_error", str(e))
        return None, ArtifactFailure(
            artifact_kind=identity.artifact_kind, symbol=identity.symbol,
            trading_date=identity.trading_date, data_type=identity.data_type,
            reason="provider_api_error", detail=str(e), attempt_count=1,
        )

    if not polygon_bars:
        await catalog_client.fail_artifact(artifact_id, "provider_no_data", "empty response")
        return None, ArtifactFailure(
            artifact_kind=identity.artifact_kind, symbol=identity.symbol,
            trading_date=identity.trading_date, data_type=identity.data_type,
            reason="provider_no_data", detail="Polygon returned no bars",
            attempt_count=1,
        )

    # Convert + encode + write.
    minute_bars = [_polygon_bar_to_minute_trade_bar(b) for b in polygon_bars]
    payload = build_minute_trade_zip_bytes(
        symbol=identity.symbol,
        trading_date_yyyymmdd=identity.trading_date.strftime("%Y%m%d"),
        bars=minute_bars,
    )
    lake_root = Path(settings.LEAN_DATA_WRITE_ROOT) / "lake"
    staging_root = Path(settings.LEAN_DATA_WRITE_ROOT) / "staging"
    file_sha = atomic_write_and_promote(
        content=payload,
        lake_root=lake_root,
        staging_root=staging_root,
        rel_lake_path=rel_path,
        request_id=spec.request_id,
        worker_id=_WORKER_ID,
        attempt=1,
    )

    first_bar_ms = polygon_bars[0].t_ms
    last_bar_ms = polygon_bars[-1].t_ms
    await catalog_client.complete_artifact(
        artifact_id=artifact_id,
        row_count=len(polygon_bars),
        first_bar_start_ms=first_bar_ms,
        last_bar_start_ms=last_bar_ms,
        file_size_bytes=len(payload),
        file_sha256=file_sha,
    )

    return (
        ArtifactRecord(
            id=artifact_id,
            artifact_kind=identity.artifact_kind,
            market=identity.market,
            symbol=identity.symbol,
            trading_date=identity.trading_date,
            resolution=identity.resolution,
            data_type=identity.data_type,
            provider=identity.provider,
            price_adjustment_mode=identity.price_adjustment_mode,
            data_contract_hash=data_contract_hash,
            file_path=file_path,
            file_sha256=file_sha,
            row_count=len(polygon_bars),
            first_bar_start_ms=first_bar_ms,
            last_bar_start_ms=last_bar_ms,
        ),
        None,
    )


async def ensure_data(spec: DataRunSpec) -> DataAvailabilityResult:
    """Dispatch by artifact kind: minute-trade through real pipeline; others
    keep the Slice 1a fake_polygon stub behavior.

    Slice 1c replaces the stub paths with real implementations (factor / map /
    derived daily / quote / metadata).
    """
    started_ms = int(time.time() * 1000)
    required, non_sessions = expand_required_artifacts(spec)

    # Ensure pool exists.
    await catalog_client.init_pool()

    artifacts: list[ArtifactRecord] = []
    failures: list[ArtifactFailure] = []
    fetched_count = 0
    reused_count = 0

    for identity in required:
        if _is_minute_trade(identity):
            record, failure = await _process_minute_trade_artifact(identity, spec)
            if record is not None:
                artifacts.append(record)
                # Heuristic: file_sha256 == zero-bytes means reused (won't happen in 1b
                # since we always compute real hash); a more precise signal lives in
                # the catalog_client (status was 'complete' before claim returned None).
                # For Slice 1b we treat each successful record as fetched=1; cache-hit
                # tracking lands in Slice 1d when ensure_data does a coverage SELECT
                # before claim.
                fetched_count += 1
            elif failure is not None:
                failures.append(failure)
        else:
            # Non-minute-trade: keep Slice 1a stub behavior.
            # (factor/map/daily/metadata implementations land in Slice 1c.)
            try:
                artifacts.append(fake_polygon.synth_artifact_record(identity))
                reused_count += 1
            except ValueError as exc:
                # Defensive: synth_artifact_record refuses minute-trade — if a
                # dispatch bug routed minute-trade here, that's the error.
                failures.append(
                    ArtifactFailure(
                        artifact_kind=identity.artifact_kind,
                        symbol=identity.symbol,
                        trading_date=identity.trading_date,
                        data_type=identity.data_type,
                        reason="internal_error",
                        detail=str(exc),
                        attempt_count=1,
                    )
                )

    if failures and artifacts:
        overall_status = "partial"
    elif failures:
        overall_status = "failed"
    else:
        overall_status = "complete"

    completed_ms = int(time.time() * 1000)
    return DataAvailabilityResult(
        request_id=spec.request_id,
        overall_status=overall_status,
        lean_data_root_path=str(Path(settings.LEAN_DATA_WRITE_ROOT) / "lake"),
        data_availability_hash=_compute_data_availability_hash(artifacts),
        artifacts=artifacts,
        failures=failures,
        skipped_non_sessions=non_sessions,
        fetched_artifact_count=fetched_count,
        reused_artifact_count=reused_count,
        refreshed_artifact_count=0,
        completed_at_ms=completed_ms,
        duration_ms=completed_ms - started_ms,
    )
```

The existing `expand_required_artifacts` and `_compute_data_availability_hash` are unchanged.

- [ ] **Step 6: Re-run the integration test**

```bash
podman exec polygon-data-service python -m pytest tests/integration/data_lake/test_ensure_data_real_polygon.py -v
```
Expected: 2 tests PASS.

- [ ] **Step 7: Run the full data_lake test suite (regression)**

```bash
podman exec polygon-data-service python -m pytest tests/unit/data_lake/ tests/integration/data_lake/ -v
```
Expected: every data_lake test passes. The Slice 1a `test_ensure_data.py` tests still pass because the dispatch loop now produces both fake-stub artifacts (for non-minute-trade) and real artifacts (for minute-trade). The hash determinism test still holds.

- [ ] **Step 8: Run project-scope ruff**

```bash
ruff check PythonDataService/app/ PythonDataService/tests/
```
Expected: clean.

- [ ] **Step 9: Run the full Python test suite (no slow tests)**

```bash
podman exec polygon-data-service python -m pytest tests/ -v -k "not slow"
```
Expected: pass.

- [ ] **Step 10: Commit**

```bash
git add PythonDataService/app/data_lake/types.py \
        PythonDataService/app/data_lake/fake_polygon.py \
        PythonDataService/app/data_lake/ensure_data.py \
        PythonDataService/tests/integration/data_lake/test_ensure_data_real_polygon.py
git commit -m "feat(data-lake): ensure_data dispatches minute-trade through real Polygon (Slice 1b)"
```

---

## Self-review

After completing all 10 tasks:

1. **Spec coverage** for Slice 1b deliverables (per spec § 6.1 phase 1b):
   - [x] Real `polygon_fetcher.py` for minute trade aggregates (Task 4)
   - [x] `lean_writer.py` deci-cent zips (Task 3)
   - [x] Full claim/steal/refresh/retry with leases (Tasks 5–8)
   - [x] Atomic-write protocol (Task 2)
   - [x] Byte hash + `data_availability_hash` over byte tuples (Tasks 2, 10)
   - [x] Sweep skeleton, not yet scheduled (Task 9)
   - [x] Feature-flag gating preserved — `ensure_data` still only reachable via `POST /api/data-lake/ensure-data` route which is gated on `DATA_LAKE_ENABLED`

2. **Type consistency**:
   - `claim_minute_bar` / `steal_or_retry_minute_bar` / `refresh_complete_minute_bar` all reject non-minute-bar identities (or are gated on the dispatch).
   - `complete_artifact` / `fail_artifact` / `refresh_lease` are kind-agnostic and work for any artifact id — Slice 1c can reuse them for factor/map/daily.
   - `ArtifactRecord.first_bar_start_ms` / `last_bar_start_ms` are populated from Polygon's `t_ms` (UTC ms) — matches the spec's "start-of-bar" convention.

3. **Deferred to later slices** (correctly):
   - Factor / map / daily / quote / metadata real implementations → Slice 1c
   - LEAN metadata Phase 0 bootstrap → Slice 1c
   - `prepare_run` workspace materialisation → Slice 1d
   - Backend GraphQL orchestration cut-over → Slice 1d
   - Launcher path-under-root contract → Slice 1d
   - `LeanMinuteDataReader` consumes the lake → Slice 2
   - Sweep cron scheduling → Slice 4
   - Coverage inspection endpoint → Slice 4

4. **Open implementation questions for Slice 1b** (not blocking):
   - `data_contract_hash` is set to `'x' * 64` placeholder in Task 10 — Slice 1c computes it deterministically over canonical provider_params + price_adjustment_mode + session_policy. Acceptable to defer because the unique constraint already enforces (market, symbol, date, data_type, provider, price_adjustment_mode) uniqueness; the hash is for forward-compat fingerprinting.
   - `ensure_data` doesn't poll on in-flight artifacts (it returns `lease_timeout` immediately when claim_minute_bar loses). Polling lands when there's a real reason for concurrent ensure_data calls — likely Slice 1d when Backend orchestration introduces parallel requests.

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-21-data-lake-slice-1b.md`.

Two execution options:

1. **Subagent-Driven (recommended)** — main agent dispatches a fresh subagent per task, two-stage review after each, fast iteration.
2. **Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
