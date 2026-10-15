"""Policy vocabulary and root resolution for both backtest engines.

This module was the policy-keyed on-disk bar store: one write boundary
(Polygon -> the exporter that lived in ``polygon_bars.py``), three readers,
and a cache tree keyed by
the DataPolicy dimensions that change bytes. #1893 retired that store --
the lake is the only place historical bars live now (ADR 0049) -- and what
survives here is the vocabulary and the root lookup its callers still need:

- :func:`policy_key` names a ``(source, adjusted)`` pair. It no longer
  selects a directory; it is the label the engine and ``/api/engine/chart``
  report so a caller can see which policy produced a response.
- :func:`resolve_data_roots` answers "where do the LEAN readers look?" with
  the lake root for the run's adjustment mode. It is the single
  root-resolution seam, which the tombstone test pins: nothing in the tree
  resolves bar data outside the lake.
- :func:`snapshot_minute_trade_zips` hashes a symbol's minute zips for the
  run manifest.

``session`` is deliberately not part of the policy: the store always held
the full session and both engines filter at read time (see
``LeanMinuteDataReader.session``). Consolidation timeframe is a downstream
concern and never touches the stored bytes. That is still true of the lake.

Concurrency is no longer this module's problem. Writers used to hold a
``symbol_write_lock`` across a check-fetch-write sequence; the lake
coordinates its writers through the catalog's claim/lease protocol with a
fencing generation (#1888) instead.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from app.data_lake import path_policy
from app.data_lake.types import polygon_mode_for

logger = logging.getLogger(__name__)

BarSource = Literal["polygon"]
# v2: the receipt's files also carry the factor file a compatibility run's
# dividends are derived from, so the fixture identity covers every input
# that affects the run, not the minute zips alone (#3000 review).
COMPATIBILITY_FIXTURE_SCHEMA_VERSION = 2
COMPATIBILITY_FIXTURE_ID_PREFIX = "bar-store-v1-"


def policy_key(*, source: BarSource, adjusted: bool) -> str:
    """Derive the cache-subtree key from the byte-changing policy dims."""
    return f"{source}-{'adjusted' if adjusted else 'raw'}"


def resolve_data_roots(*, source: BarSource, adjusted: bool) -> list[Path]:
    """Return the ordered reader roots for a policy: always ``[lake root]``.

    The lake is the market-data authority and the sole root (#1893 retired
    the flag that selected it against the pre-lake store): its tree is already
    LEAN-format, so the readers are unchanged. No reference mount is stacked
    in front — a run must be able to say which bytes it consumed, and a
    fixture silently outranking the lake would make the manifest fingerprint
    recorded on the run a lie. Both engines and the bars endpoint resolve
    roots through this single function so they always observe the same bytes.
    The lake root is created if missing. The policy key does not select it:
    the lake is single-source and keys only by adjustment
    (``path_policy.resolve_lake_root``).

    ``adjusted`` selects the lake root rather than being refused by it. It
    used to raise ``LakeAdjustmentUnsupportedError`` here, because the lake
    held one data contract per bar and it was raw — returning the raw root to
    an adjusted request would have handed a run raw prices while it believed
    it read adjusted ones, materially wrong across a split. #1866 made the
    adjustment mode a segment of the root, so the honest answer is now a
    different directory instead of a refusal.
    """
    root = path_policy.resolve_lake_root(polygon_mode_for(adjusted))
    root.mkdir(parents=True, exist_ok=True)
    return [root]


def snapshot_minute_trade_zips(
    roots: Sequence[Path],
    *,
    symbol: str,
    start: date,
    end: date,
    adjusted: bool,
    session: Literal["regular", "extended"],
) -> dict[str, Any]:
    """Hash the exact reference-first minute zips a Python run will read.

    The digest excludes host paths and wall-clock data. A LEAN companion can
    therefore resolve the same logical days from the shared store, recompute
    the digest, and reject the run if any byte changed after the Python run.
    """
    if start > end:
        raise ValueError(f"snapshot start must be <= end; got {start}..{end}")
    safe_symbol = _safe_symbol(symbol)
    logical_root = Path("equity") / "usa" / "minute" / safe_symbol.lower()
    files: list[dict[str, Any]] = []
    current = start
    while current <= end:
        filename = f"{current.strftime('%Y%m%d')}_trade.zip"
        source: Path | None = None
        for root in roots:
            root_real = os.path.realpath(os.fspath(root))
            root_prefix = root_real.rstrip(os.sep) + os.sep
            candidate_real = os.path.realpath(
                os.path.join(root_real, os.fspath(logical_root), filename)
            )
            if not candidate_real.startswith(root_prefix):
                continue
            candidate = Path(candidate_real)
            if candidate.is_file():
                source = candidate
                break
        if source is not None:
            payload = source.read_bytes()
            files.append(
                {
                    "trading_date": current.isoformat(),
                    "path": (logical_root / filename).as_posix(),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                }
            )
        current += timedelta(days=1)
    if not files:
        raise FileNotFoundError(f"no minute trade zips for {safe_symbol} in {start}..{end}")

    # The factor file beside the zips is an input the run reads too — the
    # dividends the LEAN-compatibility profile pays are derived from it
    # (#2903) — so it belongs inside the one receipt the fixture identity
    # digests: two runs on identical bars must not share an identity while
    # paying different dividends (#3000 review). A tree with no factor
    # file receipts none; a run then pays no dividends, exactly as before.
    from app.engine.data.dividends import find_factor_file

    found = find_factor_file(roots, safe_symbol)
    if found is not None:
        factor_file, relative = found
        payload = factor_file.read_bytes()
        files.append(
            {
                "path": relative.as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        )

    receipt: dict[str, Any] = {
        "schema_version": COMPATIBILITY_FIXTURE_SCHEMA_VERSION,
        "symbol": safe_symbol,
        "adjusted": adjusted,
        "session": session,
        "files": files,
    }
    canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return receipt | {
        "fixture_id": f"{COMPATIBILITY_FIXTURE_ID_PREFIX}{digest[:16]}",
        "fixture_sha256": digest,
    }


def _safe_symbol(symbol: str) -> str:
    """Validate the symbol before it flows into a filesystem path."""
    # Lazy import: the canonical path-safety validator lives with the
    # sidecar workspace code; the engine layer reuses it rather than
    # duplicating the ticker alphabet.
    from app.lean_sidecar.workspace import validate_symbol

    return validate_symbol(symbol)
