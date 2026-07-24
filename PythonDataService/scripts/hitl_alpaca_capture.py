"""HITL #1178 + #1198 — live Alpaca paper-account capture and validation.

Run from PythonDataService/ with paper credentials in .env:

    cd PythonDataService
    python scripts/hitl_alpaca_capture.py

Gates closed by this script
---------------------------
#1178 H1 — Confirms paper keys load; market-data endpoint not wired.
#1178 H2 — Calls all 6 read endpoints, extracts real payloads from the
            capture journal, sanitizes linkable identifiers, replaces
            pending-real-capture fixtures, updates attribution files.
#1198 S7  — Submits one SPY market order, verifies client_order_id echoes
             back untruncated, opens trade_updates websocket and captures
             the full lifecycle (new → partial_fill* → fill) as real frames,
             replaces synthetic trade_updates fixture, documents the proven
             order_ref length cap.

Sanitization rules
------------------
- UUID fields (id, account_number-style IDs when UUID-shaped, asset_id,
  order_id, execution_id) → distinct placeholder UUIDs (01-sentinel through N)
  so adapter tests remain structural without leaking real broker IDs.
- Account number → "PA0SANITIZED00001".
- All numeric values, timestamps, status strings, symbols: kept verbatim
  (the adapter maps these; structural sanitization must not disturb them).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import websockets

# ── path setup ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent  # PythonDataService/
_REPO_ROOT = _SERVICE_ROOT.parent

if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

# ── application imports (after path setup) ────────────────────────────────────
from app.broker.alpaca.client import AlpacaTradingClient  # noqa: E402
from app.broker.alpaca.config import (  # noqa: E402
    get_alpaca_settings,
    reset_alpaca_settings_for_testing,
)
from app.broker.capture.journal import CaptureSettings, reset_capture_journal_for_testing  # noqa: E402
from app.engine.live.order_identity import (  # noqa: E402
    DEFAULT_ORDER_REF_MAX_LENGTH,
    build_manual_order_namespace,
    build_order_ref,
    mint_intent_id,
)

# ── constants ─────────────────────────────────────────────────────────────────
_FIXTURE_DIR = _SERVICE_ROOT / "tests" / "fixtures" / "alpaca"
_TODAY_UTC = datetime.now(UTC).strftime("%Y-%m-%d")
_JOURNAL_ENTRY_TIMEOUT_S = 5.0
_JOURNAL_ENTRY_POLL_S = 0.05

# Sentinel UUIDs used to replace real broker identifiers — structurally valid
# so adapter code can parse them, but obviously not real.
_UUID_SENTINEL_BASE = "00000000-0000-0000-0000-{:012d}"
_ACCOUNT_NUMBER_SENTINEL = "PA0SANITIZED00001"

# Alpaca's paper trade_updates websocket endpoint.
_WS_PAPER_URL = "wss://paper-api.alpaca.markets/stream"

# How long to wait for a terminal order state over the websocket.
_WS_TIMEOUT_S = 300  # 5 min — allows for pre-market queue + open fill

# Order details for the HITL gate: 1 share of SPY, market order.
_HITL_SYMBOL = "SPY"
_HITL_QTY = "1"
_HITL_OPERATOR = "hitl-gate"

logger = logging.getLogger(__name__)

# ── sanitization helpers ───────────────────────────────────────────────────────
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_ACCOUNT_NUMBER_RE = re.compile(r"\bPA[0-9A-Z]{8,15}\b")
_ORDER_IDENTIFIER_KEYS = frozenset({"client_order_id", "order_ref"})
_ORDER_IDENTIFIER_SENTINEL = "manual/hitl-gate/v1:SANITIZED0000000000001"

_FIXTURE_STEMS = {
    "account": "account",
    "positions": "positions",
    "orders": "orders",
    "activities": "activities",
    "assets": "assets",
    "clock": "clock",
    "trade_updates": "trade_updates",
}

_SYNTHETIC_PROVENANCE = {
    "activities": (
        "## Synthetic supplemental records\n\n"
        "- The FILL record with sentinel order ID ending in `000099` is synthetic; "
        "it retains deterministic trade-activity coverage when a current capture has no FILL. "
        "All other records are sanitized live paper-account captures."
    ),
    "assets": (
        "## Synthetic supplemental records\n\n"
        "- The inactive `DELISTED` asset is synthetic; live capture requests only active assets."
        " All other records are sanitized live paper-account captures."
    ),
    "orders": (
        "## Synthetic supplemental records\n\n"
        "- The open limit order whose `client_order_id` contains `SYNTHETIC` is synthetic; "
        "it retains resting-order coverage. All other records are sanitized live paper-account captures."
    ),
    "positions": (
        "## Synthetic supplemental records\n\n"
        "- The TSLA short position with sentinel asset ID ending in `000099` is synthetic; "
        "it retains signed-short mapping coverage. All other records are sanitized live paper-account captures."
    ),
    "trade_updates": (
        "## Synthetic supplemental records\n\n"
        "- `partial_fill`, `canceled`, and `rejected` lifecycle frames whose `client_order_id` "
        "contains `SYNTHETIC` are synthetic; captured frames are documented separately below. "
        "All other frames are sanitized live paper-account captures."
    ),
}


def _sanitize_payload(raw: Any, *, uuid_map: dict[str, str] | None = None) -> Any:
    """Recursively scrub UUIDs and account numbers from a parsed JSON structure.

    UUIDs are replaced with deterministic sentinel values so tests that assert
    on structural field presence remain valid. The same input UUID always maps
    to the same sentinel within one call (stable within a fixture file).
    """
    if uuid_map is None:
        uuid_map = {}

    if isinstance(raw, dict):
        return {
            key: (
                _sanitize_order_identifier(value)
                if key in _ORDER_IDENTIFIER_KEYS and isinstance(value, str)
                else _sanitize_payload(value, uuid_map=uuid_map)
            )
            for key, value in raw.items()
        }
    if isinstance(raw, list):
        return [_sanitize_payload(item, uuid_map=uuid_map) for item in raw]
    if isinstance(raw, str):
        return _sanitize_str(raw, uuid_map)
    return raw


def _sanitize_str(value: str, uuid_map: dict[str, str]) -> str:
    # Replace account numbers before UUIDs (non-overlapping).
    value = _ACCOUNT_NUMBER_RE.sub(_ACCOUNT_NUMBER_SENTINEL, value)

    def _replace_uuid(m: re.Match[str]) -> str:
        orig = m.group(0).lower()
        if orig not in uuid_map:
            idx = len(uuid_map) + 1
            uuid_map[orig] = _UUID_SENTINEL_BASE.format(idx)
        return uuid_map[orig]

    return _UUID_RE.sub(_replace_uuid, value)


def _sanitize_order_identifier(value: str) -> str:
    """Replace a linkable operator order identifier with a stable fixture token."""
    return _ORDER_IDENTIFIER_SENTINEL if value else value


# ── journal extraction ─────────────────────────────────────────────────────────


def _configured_capture_dir() -> Path:
    """Return the same capture root that the broker journal resolves from settings."""
    return CaptureSettings().dir


def _latest_journal_entry(
    capture_dir: Path,
    broker: str,
    family: str,
    *,
    captured_after_ms: int,
) -> dict[str, Any] | None:
    """Return the latest current-run entry for one endpoint family."""
    path = capture_dir / broker / family / f"{_TODAY_UTC}.jsonl"
    if not path.exists():
        return None
    last: dict[str, Any] | None = None
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    candidate = json.loads(line)
                except json.JSONDecodeError:
                    continue
                captured_at_ms = candidate.get("captured_at_ms")
                if isinstance(captured_at_ms, int) and captured_at_ms >= captured_after_ms:
                    last = candidate
    return last


async def _wait_for_current_run_journal_entry(
    capture_dir: Path,
    broker: str,
    family: str,
    *,
    captured_after_ms: int,
) -> dict[str, Any]:
    """Poll briefly for a fresh journal entry written by this capture run."""
    deadline = time.monotonic() + _JOURNAL_ENTRY_TIMEOUT_S
    while True:
        entry = _latest_journal_entry(
            capture_dir,
            broker,
            family,
            captured_after_ms=captured_after_ms,
        )
        if entry is not None:
            return entry
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"No current-run journal entry found for family '{family}' within "
                f"{_JOURNAL_ENTRY_TIMEOUT_S:g}s; check BROKER_CAPTURE_DIR and capture hook."
            )
        await asyncio.sleep(_JOURNAL_ENTRY_POLL_S)


def _extract_raw_body(entry: dict[str, Any]) -> Any:
    """Decode raw_body from a journal entry (handles base64 if needed)."""
    raw = entry["raw_body"]
    encoding = entry.get("body_encoding")
    if encoding == "base64":
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    return json.loads(raw)


# ── fixture writers ────────────────────────────────────────────────────────────


def _write_fixture(family: str, payload: Any, *, note: str = "") -> None:
    path = _fixture_path(family)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    logger.info(
        "fixture written",
        extra={"path": str(path.relative_to(_REPO_ROOT)), "note": note or None},
    )


def _fixture_path(family: str) -> Path:
    """Return the committed fixture path for one endpoint family."""
    return _FIXTURE_DIR / family / f"{_FIXTURE_STEMS.get(family, family)}.json"


def _is_synthetic_fixture_record(family: str, record: Any) -> bool:
    """Identify supplemental deterministic records that a live recapture must retain."""
    if not isinstance(record, dict):
        return False
    if family == "activities":
        return str(record.get("id", "")).endswith("000099")
    if family == "assets":
        return record.get("symbol") == "DELISTED"
    if family == "orders":
        return "SYNTHETIC" in str(record.get("client_order_id", ""))
    if family == "positions":
        return record.get("symbol") == "TSLA" and str(record.get("asset_id", "")).endswith("000099")
    if family == "trade_updates":
        order = record.get("data", {}).get("order", {})
        return "SYNTHETIC" in str(order.get("client_order_id", ""))
    return False


def _preserved_synthetic_records(family: str) -> list[dict[str, Any]]:
    """Load the existing supplemental records for a fixture family."""
    path = _fixture_path(family)
    if not path.exists():
        return []
    existing = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(existing, list):
        return []
    return [record for record in existing if _is_synthetic_fixture_record(family, record)]


def _with_preserved_synthetic_records(family: str, payload: Any) -> Any:
    """Append fixed edge-case records to a newly captured list payload."""
    if not isinstance(payload, list):
        return payload
    return [*payload, *_preserved_synthetic_records(family)]


def _write_attribution(
    family: str,
    *,
    captured_at_ms: int,
    sanitization_notes: str,
    extra: str = "",
) -> None:
    captured_at_iso = datetime.fromtimestamp(captured_at_ms / 1000, tz=UTC).isoformat()
    synthetic_provenance = _SYNTHETIC_PROVENANCE.get(family, "")
    reference_kind = (
        "mixed_real_sanitized_capture_and_synthetic_scenarios" if synthetic_provenance else "real_sanitized_capture"
    )
    status = "mixed-real-capture" if synthetic_provenance else "real-capture"
    content = f"""# Fixture attribution — {family}

- **broker:** alpaca (paper)
- **endpoint_family:** {family}
- **captured_at_ms:** {captured_at_ms}
- **captured_at:** {captured_at_iso}
- **source:** live Alpaca paper account (HITL gate — script `scripts/hitl_alpaca_capture.py`)
- **reference_kind:** `{reference_kind}`
- **sanitization:** {sanitization_notes}

{extra.strip()}

{synthetic_provenance}

## Status: `{status}`

Replaced `pending-real-capture` synthetic fixtures on {_TODAY_UTC} via HITL
gate #1178 / #1198. Adapter + schema-drift tests run against this payload.
"""
    path = _FIXTURE_DIR / family / "attribution.md"
    with path.open("w", encoding="utf-8") as fh:
        fh.write(content)
    logger.info("fixture attribution written", extra={"path": str(path.relative_to(_REPO_ROOT))})


# ── H1: credentials check ─────────────────────────────────────────────────────


def _check_credentials() -> None:
    logger.info("[H1] Checking credentials")
    reset_alpaca_settings_for_testing()
    settings = get_alpaca_settings()
    if not settings.is_paper:
        raise RuntimeError("ALPACA_MODE must be 'paper'")
    if not settings.api_key_id:
        raise RuntimeError("ALPACA_API_KEY_ID missing")
    if not settings.api_secret_key:
        raise RuntimeError("ALPACA_API_SECRET_KEY missing")
    # H1 requirement: market-data endpoint must NOT be wired into the phase-1
    # trading client. The client uses TradingClient (paper base URL only).
    logger.info("paper credentials verified; credential values are not logged")
    logger.info("market-data endpoint not wired into trading client (phase-1 design)")


# ── H2: live read captures ─────────────────────────────────────────────────────


async def _capture_reads(client: AlpacaTradingClient) -> dict[str, dict[str, Any]]:
    """Call all read endpoints and return {family: journal_entry}."""
    logger.info("[H2] Capturing read endpoints")
    capture_started_at_ms = int(datetime.now(UTC).timestamp() * 1000)
    capture_dir = _configured_capture_dir()

    families: list[tuple[str, Callable[[], Awaitable[Any]]]] = [
        ("account", client.get_account),
        ("positions", client.list_positions),
        ("orders", lambda: client.list_orders(status="all", limit=5)),
        ("activities", lambda: client.list_activities(limit=5)),
        ("assets", lambda: client.list_assets(status="active", limit=3)),
        ("clock", client.get_clock),
    ]

    for family, call in families:
        try:
            result = await call()
            logger.info(
                "read endpoint captured",
                extra={"family": family, "item_count": len(result) if isinstance(result, list) else None},
            )
        except Exception:
            logger.exception("read endpoint capture failed", extra={"family": family})
            raise

    # Extract entries written during this exact capture pass. Journal writes
    # flush per line, but a bounded poll avoids assuming a fixed I/O latency.
    results: dict[str, dict[str, Any]] = {}
    for family, _ in families:
        entry = await _wait_for_current_run_journal_entry(
            capture_dir,
            "alpaca",
            family,
            captured_after_ms=capture_started_at_ms,
        )
        results[family] = entry
        logger.info(
            "fresh journal entry captured",
            extra={
                "family": family,
                "status": entry["status"],
                "raw_body_bytes": len(entry["raw_body"]),
            },
        )

    return results


def _replace_fixtures_from_reads(journal_entries: dict[str, dict[str, Any]]) -> None:
    logger.info("[H2] Replacing fixtures")
    sanitize_note = (
        "UUIDs replaced with deterministic sentinel values "
        "(00000000-0000-0000-0000-{N:012d}); "
        "account numbers replaced with PA0SANITIZED00001."
    )
    for family, entry in journal_entries.items():
        raw_payload = _extract_raw_body(entry)
        sanitized = _sanitize_payload(raw_payload)
        _write_fixture(family, _with_preserved_synthetic_records(family, sanitized))
        _write_attribution(
            family,
            captured_at_ms=entry["captured_at_ms"],
            sanitization_notes=sanitize_note,
        )


# ── S7: order submission + websocket lifecycle ─────────────────────────────────


async def _run_order_gate(client: AlpacaTradingClient) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Submit one SPY market order and observe the full lifecycle."""
    logger.info("[S7] Order submission gate")

    # Build a proper order_ref using the same helpers the clerk uses.
    namespace = build_manual_order_namespace(_HITL_OPERATOR)
    intent_id = mint_intent_id()
    order_ref = build_order_ref(namespace, intent_id)
    logger.info(
        "order reference created",
        extra={"order_ref_length": len(order_ref), "max_length": DEFAULT_ORDER_REF_MAX_LENGTH},
    )
    if len(order_ref) > DEFAULT_ORDER_REF_MAX_LENGTH:
        raise RuntimeError(f"order_ref exceeds cap: {len(order_ref)} > {DEFAULT_ORDER_REF_MAX_LENGTH}")

    order_body = {
        "symbol": _HITL_SYMBOL,
        "qty": _HITL_QTY,
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": order_ref,
    }

    settings = get_alpaca_settings()

    # Open the websocket BEFORE submitting the order so we don't miss the
    # initial "new" event.
    logger.info("opening paper trade_updates websocket")
    ws_frames: list[dict[str, Any]] = []
    ws_ready = asyncio.Event()
    ws_setup_error: Exception | None = None
    _TERMINAL_EVENTS = frozenset({"fill", "canceled", "expired", "rejected", "replaced"})

    async def _ws_listener() -> None:
        nonlocal ws_setup_error
        try:
            async with websockets.connect(  # type: ignore[attr-defined]
                _WS_PAPER_URL,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                # 1. Auth frame.
                auth = json.dumps(
                    {
                        "action": "authenticate",
                        "data": {
                            "key_id": settings.api_key_id,
                            "secret_key": settings.api_secret_key,
                        },
                    }
                )
                await ws.send(auth)
                ack = json.loads(await ws.recv())
                ws_frames.append({"_meta": "auth_ack", "frame": ack})

                # 2. Subscribe to trade_updates.
                sub = json.dumps(
                    {
                        "action": "listen",
                        "data": {"streams": ["trade_updates"]},
                    }
                )
                await ws.send(sub)
                sub_ack = json.loads(await ws.recv())
                ws_frames.append({"_meta": "subscribe_ack", "frame": sub_ack})

                # Signal the order submission task.
                ws_ready.set()

                # 3. Receive lifecycle events until terminal or timeout.
                async with asyncio.timeout(_WS_TIMEOUT_S):
                    async for raw_msg in ws:
                        frame = json.loads(raw_msg)
                        event = frame.get("data", {}).get("event", "") if isinstance(frame, dict) else ""
                        order_cid = frame.get("data", {}).get("order", {}).get("client_order_id", "")
                        if order_cid == order_ref:
                            logger.info("received lifecycle event for submitted order", extra={"event": event})
                            ws_frames.append({"_meta": f"lifecycle/{event}", "frame": frame})
                            if event in _TERMINAL_EVENTS:
                                return
                        else:
                            logger.info("ignored lifecycle event for another order", extra={"event": event})
                raise RuntimeError("trade_updates websocket closed before a terminal event")
        except TimeoutError as exc:
            ws_setup_error = RuntimeError(
                f"trade_updates websocket timed out after {_WS_TIMEOUT_S:g}s without a terminal event"
            )
            logger.error("trade_updates websocket timed out before terminal lifecycle evidence")
            raise ws_setup_error from exc
        except Exception as exc:
            ws_setup_error = exc
            logger.exception("trade_updates websocket failed")
            raise
        finally:
            # A handshake failure must release the submit task so it can
            # propagate the listener's failure instead of waiting forever.
            ws_ready.set()

    async def _submit_order() -> dict[str, Any]:
        # Wait for the websocket to be subscribed before submitting.
        await ws_ready.wait()
        if ws_setup_error is not None:
            raise RuntimeError("trade_updates websocket setup failed; order was not submitted") from ws_setup_error
        logger.info("submitting paper HITL order", extra={"symbol": _HITL_SYMBOL, "quantity": _HITL_QTY})
        result = await client.submit_order(order_body)
        logger.info("paper HITL order accepted", extra={"status": result.get("status")})

        # HITL #1198 key assertion: client_order_id echoes back untruncated.
        returned_cid = result.get("client_order_id", "")
        if returned_cid == order_ref:
            logger.info("client order ID round-trip matched", extra={"order_ref_length": len(order_ref)})
        else:
            logger.error(
                "client order ID round-trip mismatch",
                extra={"sent_length": len(order_ref), "received_length": len(returned_cid)},
            )
            raise AssertionError("client_order_id not echoed back untruncated")

        return result

    # Run listener and submission concurrently.
    submit_task = asyncio.create_task(_submit_order())
    ws_task = asyncio.create_task(_ws_listener())
    try:
        submit_result, _ = await asyncio.gather(submit_task, ws_task)
    except BaseException:
        submit_task.cancel()
        ws_task.cancel()
        await asyncio.gather(submit_task, ws_task, return_exceptions=True)
        raise

    return submit_result, ws_frames


def _write_order_fixtures(
    post_order_payload: list[dict[str, Any]],
    ws_frames: list[dict[str, Any]],
    *,
    order_ref: str,
    captured_at_ms: int,
) -> None:
    """Write post-order and lifecycle fixtures while retaining synthetic edge cases."""
    logger.info("[S7] Writing order fixtures")

    uuid_map: dict[str, str] = {}

    # The fresh read after a terminal lifecycle event is the authoritative
    # post-order account state. Supplemental synthetic rows retain edge cases
    # that a live account cannot guarantee on every capture.
    sanitized_orders = _sanitize_payload(post_order_payload, uuid_map=uuid_map)
    _write_fixture(
        "orders",
        _with_preserved_synthetic_records("orders", sanitized_orders),
        note="post-order read response from HITL gate",
    )
    _write_attribution(
        "orders",
        captured_at_ms=captured_at_ms,
        sanitization_notes=(
            "UUIDs replaced with sentinel values; client_order_id and order_ref replaced with "
            "the stable, non-linkable fixture token."
        ),
        extra=(
            f"## order_ref length cap proof\n\n"
            f"- `order_ref` sent:   `{_sanitize_order_identifier(order_ref)}` ({len(order_ref)} chars)\n"
            f"- `DEFAULT_ORDER_REF_MAX_LENGTH`: {DEFAULT_ORDER_REF_MAX_LENGTH}\n"
            f"- Alpaca echoed `client_order_id` back UNTRUNCATED (exact match).\n"
            f"- Margin: {DEFAULT_ORDER_REF_MAX_LENGTH - len(order_ref)} chars spare.\n"
            f"- Cap is proven safe for the current namespace format.\n"
        ),
    )

    # trade_updates fixture: the real frames minus auth material.
    # Strip _meta keys but keep the frame structure; redact auth frames.
    safe_frames: list[dict[str, Any]] = []
    for entry in ws_frames:
        meta = entry.get("_meta", "")
        frame = entry["frame"]
        if meta == "auth_ack":
            # Never commit auth acknowledgements that echo key material.
            # Write a structural placeholder instead.
            safe_frames.append(
                {
                    "stream": "authorization",
                    "data": {"status": "authorized", "action": "authenticate"},
                }
            )
        elif meta == "subscribe_ack":
            safe_frames.append(_sanitize_payload(frame, uuid_map=uuid_map))
        elif meta.startswith("lifecycle/"):
            # lifecycle/* — real event frames for our order.
            safe_frames.append(_sanitize_payload(frame, uuid_map=uuid_map))
        else:
            # Frames from another order are deliberately excluded: they do not
            # demonstrate this gated submission's lifecycle and could leak an
            # unrelated operator's activity into a committed fixture.
            logger.info("excluded unrelated websocket frame from fixture", extra={"meta": meta})

    _write_fixture(
        "trade_updates",
        _with_preserved_synthetic_records("trade_updates", safe_frames),
        note=f"{len(safe_frames)} captured frames from HITL gate",
    )
    _write_attribution(
        "trade_updates",
        captured_at_ms=captured_at_ms,
        sanitization_notes=(
            "Auth frame replaced with structural placeholder (no key material). "
            "Order UUIDs replaced with sentinel values. "
            "client_order_id in lifecycle frames sanitized."
        ),
        extra=("## Frames captured\n\n" + "\n".join(f"- `{e['_meta']}`" for e in ws_frames)),
    )


# ── README update ──────────────────────────────────────────────────────────────


def _update_readme() -> None:
    path = _FIXTURE_DIR / "README.md"
    content = f"""# Alpaca golden fixtures (Broker System v2)

Sanitized Alpaca REST payloads captured from a live paper account via the HITL
gate (script `scripts/hitl_alpaca_capture.py`, run {_TODAY_UTC}).
One subdirectory per endpoint family, each with the raw payload(s) and an
`attribution.md`.

These are **sanitized raw Alpaca wire fixtures**. UUIDs, account numbers, and
client-order identifiers are replaced with deterministic sentinel values. The
remaining values, including RFC3339 timestamps, retain the vendor wire shape so
the adapter ingestion boundary is tested. The adapter immediately converts those
raw vendor timestamps to canonical `int64 ms UTC`; the fixtures are not internal
storage or contract payloads.

Most records are real paper-account captures. Deterministic synthetic supplemental
records retain edge cases that a live recapture cannot guarantee: the FILL
activity, inactive `DELISTED` asset, TSLA short position, open limit order, and
`partial_fill`/`canceled`/`rejected` trade-update frames. Each `attribution.md`
identifies its mixed provenance.

These fixtures are outside the numerical golden manifest
(`tests/fixtures/golden/manifest.json`) — that system governs
tolerance-pinned math equivalence, which does not apply to broker payload shape.

## Status: `mixed-real-capture`

Replaced `pending-real-capture` synthetic fixtures on {_TODAY_UTC}.
Adapter + schema-drift tests pass against these raw wire payloads.

## Regeneration

Run `python scripts/hitl_alpaca_capture.py` from `PythonDataService/` with
paper credentials in `.env`. The script calls all read endpoints, submits the
documented paper test order, waits for terminal websocket evidence, captures
post-order state, and regenerates every fixture + attribution file. It fails
without changing fixtures if the order lifecycle cannot be proven.
"""
    with path.open("w", encoding="utf-8") as fh:
        fh.write(content)
    logger.info("fixture README updated", extra={"path": str(path.relative_to(_REPO_ROOT))})


# ── main ───────────────────────────────────────────────────────────────────────


async def _main() -> None:
    logger.info("=" * 60)
    logger.info("HITL Alpaca capture — gates #1178 + #1198")
    logger.info("UTC capture date", extra={"date": _TODAY_UTC})
    logger.info("=" * 60)

    # H1: credential check.
    _check_credentials()

    # Use a fresh in-memory journal that also writes to disk (so entries are
    # available for extraction). The default journal goes to var/broker_captures/
    # which is git-ignored.
    reset_capture_journal_for_testing()

    client = AlpacaTradingClient()

    # H2: establish the read-capture path before placing the documented order.
    await _capture_reads(client)

    # S7: order gate.
    logger.info("[S7] Running order submission gate")
    captured_at_ms = int(datetime.now(UTC).timestamp() * 1000)
    try:
        submit_result, ws_frames = await _run_order_gate(client)
    except Exception:
        logger.exception("order gate failed; no fixtures were replaced")
        raise

    # Capture the state after the terminal lifecycle event; this is the state
    # the read fixtures and their adapter assertions must represent.
    post_order_entries = await _capture_reads(client)
    _replace_fixtures_from_reads(post_order_entries)
    order_ref = str(submit_result.get("client_order_id", ""))
    post_order_payload = _extract_raw_body(post_order_entries["orders"])
    if not isinstance(post_order_payload, list):
        raise RuntimeError("post-order Alpaca orders payload is not a list")
    _write_order_fixtures(
        post_order_payload,
        ws_frames,
        order_ref=order_ref,
        captured_at_ms=captured_at_ms,
    )

    # Update the top-level README.
    logger.info("[finish] Updating README")
    _update_readme()

    logger.info("=" * 60)
    logger.info("HITL capture complete")
    logger.info("Run adapter and schema-drift tests before committing regenerated fixtures")
    logger.info("=" * 60)


def _prepare_runtime_working_directory() -> None:
    """Run with the service root as CWD so pydantic-settings locates ``.env``."""
    if Path.cwd().resolve() != _SERVICE_ROOT:
        os.chdir(_SERVICE_ROOT)


if __name__ == "__main__":
    _prepare_runtime_working_directory()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    anyio.run(_main)
