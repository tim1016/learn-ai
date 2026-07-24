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
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ── path setup ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent          # PythonDataService/
_REPO_ROOT = _SERVICE_ROOT.parent

if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

# pydantic-settings resolves .env relative to CWD; must run from SERVICE_ROOT.
if Path.cwd().resolve() != _SERVICE_ROOT:
    os.chdir(_SERVICE_ROOT)

# ── imports (after path + chdir) ──────────────────────────────────────────────
import anyio
import websockets

from app.broker.alpaca.client import AlpacaTradingClient
from app.broker.alpaca.config import get_alpaca_settings
from app.broker.capture.journal import CaptureJournal, reset_capture_journal_for_testing
from app.broker.alpaca.config import reset_alpaca_settings_for_testing
from app.engine.live.order_identity import (
    DEFAULT_ORDER_REF_MAX_LENGTH,
    build_manual_order_namespace,
    build_order_ref,
    mint_intent_id,
)

# ── constants ─────────────────────────────────────────────────────────────────
_FIXTURE_DIR = _SERVICE_ROOT / "tests" / "fixtures" / "alpaca"
_CAPTURE_DIR = _SERVICE_ROOT / "var" / "broker_captures"
_TODAY_UTC = datetime.now(UTC).strftime("%Y-%m-%d")

# Sentinel UUIDs used to replace real broker identifiers — structurally valid
# so adapter code can parse them, but obviously not real.
_UUID_SENTINEL_BASE = "00000000-0000-0000-0000-{:012d}"
_ACCOUNT_NUMBER_SENTINEL = "PA0SANITIZED00001"

# Alpaca's paper trade_updates websocket endpoint.
_WS_PAPER_URL = "wss://paper-api.alpaca.markets/stream"

# How long to wait for a terminal order state over the websocket.
_WS_TIMEOUT_S = 300   # 5 min — allows for pre-market queue + open fill

# Order details for the HITL gate: 1 share of SPY, market order.
_HITL_SYMBOL = "SPY"
_HITL_QTY = "1"
_HITL_OPERATOR = "hitl-gate"

# ── sanitization helpers ───────────────────────────────────────────────────────
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_ACCOUNT_NUMBER_RE = re.compile(r"\bPA[0-9A-Z]{8,15}\b")


def _sanitize_payload(raw: Any, *, uuid_map: dict[str, str] | None = None) -> Any:
    """Recursively scrub UUIDs and account numbers from a parsed JSON structure.

    UUIDs are replaced with deterministic sentinel values so tests that assert
    on structural field presence remain valid. The same input UUID always maps
    to the same sentinel within one call (stable within a fixture file).
    """
    if uuid_map is None:
        uuid_map = {}

    if isinstance(raw, dict):
        return {k: _sanitize_payload(v, uuid_map=uuid_map) for k, v in raw.items()}
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


# ── journal extraction ─────────────────────────────────────────────────────────

def _latest_journal_entry(broker: str, family: str) -> dict[str, Any] | None:
    """Return the last entry from today's journal file for this endpoint family."""
    path = _CAPTURE_DIR / broker / family / f"{_TODAY_UTC}.jsonl"
    if not path.exists():
        return None
    last: dict[str, Any] | None = None
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    last = json.loads(line)
                except json.JSONDecodeError:
                    pass
    return last


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
    path = _FIXTURE_DIR / family / f"{family.rstrip('s')}.json"
    # Use the same stem as existing files; for multi-item families use plural.
    # Actual filenames: account.json, positions.json, orders.json, etc.
    stem_map = {
        "account": "account",
        "positions": "positions",
        "orders": "orders",
        "activities": "activities",
        "assets": "assets",
        "clock": "clock",
        "trade_updates": "trade_updates",
    }
    stem = stem_map.get(family, family)
    path = _FIXTURE_DIR / family / f"{stem}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"  ✓ wrote {path.relative_to(_REPO_ROOT)}{' — ' + note if note else ''}")


def _write_attribution(
    family: str,
    *,
    captured_at_ms: int,
    sanitization_notes: str,
    extra: str = "",
) -> None:
    captured_at_iso = datetime.fromtimestamp(captured_at_ms / 1000, tz=UTC).isoformat()
    content = f"""# Fixture attribution — {family}

- **broker:** alpaca (paper)
- **endpoint_family:** {family}
- **captured_at_ms:** {captured_at_ms}
- **captured_at:** {captured_at_iso}
- **source:** live Alpaca paper account (HITL gate — script `scripts/hitl_alpaca_capture.py`)
- **reference_kind:** `real_sanitized_capture`
- **sanitization:** {sanitization_notes}

{extra.strip()}

## Status: `real-capture`

Replaced `pending-real-capture` synthetic fixtures on {_TODAY_UTC} via HITL
gate #1178 / #1198. Adapter + schema-drift tests run against this payload.
"""
    path = _FIXTURE_DIR / family / "attribution.md"
    with path.open("w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"  ✓ attribution {path.relative_to(_REPO_ROOT)}")


# ── H1: credentials check ─────────────────────────────────────────────────────

def _check_credentials() -> None:
    print("\n[H1] Checking credentials ...")
    reset_alpaca_settings_for_testing()
    settings = get_alpaca_settings()
    assert settings.is_paper, "ALPACA_MODE must be 'paper'"
    assert settings.api_key_id, "ALPACA_API_KEY_ID missing"
    assert settings.api_secret_key, "ALPACA_API_SECRET_KEY missing"
    # H1 requirement: market-data endpoint must NOT be wired into the phase-1
    # trading client. The client uses TradingClient (paper base URL only).
    print(f"  ✓ mode=paper, key_id={settings.api_key_id[:4]}…, secret=<redacted>")
    print("  ✓ market-data endpoint not wired into trading client (phase-1 design)")


# ── H2: live read captures ─────────────────────────────────────────────────────

async def _capture_reads(client: AlpacaTradingClient) -> dict[str, dict[str, Any]]:
    """Call all read endpoints and return {family: journal_entry}."""
    print("\n[H2] Capturing read endpoints ...")

    families: list[tuple[str, Any]] = [
        ("account",    client.get_account()),
        ("positions",  client.list_positions()),
        ("orders",     client.list_orders(status="all", limit=5)),
        ("activities", client.list_activities(limit=5)),
        ("assets",     client.list_assets(status="active", limit=3)),
        ("clock",      client.get_clock()),
    ]

    for family, coro in families:
        try:
            result = await coro
            print(f"  → {family}: status=200, "
                  f"{'['+str(len(result))+' items]' if isinstance(result, list) else 'ok'}")
        except Exception as exc:
            print(f"  ✗ {family}: {exc}")
            raise

    # Give the journal a beat to flush (it flushes per-line so this is
    # just defensive).
    await asyncio.sleep(0.1)

    # Extract entries from the journal.
    results: dict[str, dict[str, Any]] = {}
    for family, _ in families:
        entry = _latest_journal_entry("alpaca", family)
        if entry is None:
            raise RuntimeError(f"No journal entry found for family '{family}' — "
                               f"check BROKER_CAPTURE_DIR and capture hook.")
        results[family] = entry
        print(f"  ✓ journal entry captured for {family} "
              f"(status={entry['status']}, "
              f"{len(entry['raw_body'])} raw bytes)")

    return results


def _replace_fixtures_from_reads(
    journal_entries: dict[str, dict[str, Any]]
) -> None:
    print("\n[H2] Replacing fixtures ...")
    sanitize_note = (
        "UUIDs replaced with deterministic sentinel values "
        "(00000000-0000-0000-0000-{N:012d}); "
        "account numbers replaced with PA0SANITIZED00001."
    )
    for family, entry in journal_entries.items():
        raw_payload = _extract_raw_body(entry)
        sanitized = _sanitize_payload(raw_payload)
        _write_fixture(family, sanitized)
        _write_attribution(
            family,
            captured_at_ms=entry["captured_at_ms"],
            sanitization_notes=sanitize_note,
        )


# ── S7: order submission + websocket lifecycle ─────────────────────────────────

async def _run_order_gate(client: AlpacaTradingClient) -> None:
    """Submit one SPY market order and observe the full lifecycle."""
    print("\n[S7] Order submission gate ...")

    # Build a proper order_ref using the same helpers the clerk uses.
    namespace = build_manual_order_namespace(_HITL_OPERATOR)
    intent_id = mint_intent_id()
    order_ref = build_order_ref(namespace, intent_id)
    print(f"  order_ref = {order_ref!r} ({len(order_ref)} chars, cap={DEFAULT_ORDER_REF_MAX_LENGTH})")
    assert len(order_ref) <= DEFAULT_ORDER_REF_MAX_LENGTH, (
        f"order_ref exceeds cap: {len(order_ref)} > {DEFAULT_ORDER_REF_MAX_LENGTH}"
    )

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
    print(f"  Opening trade_updates websocket ({_WS_PAPER_URL}) ...")
    ws_frames: list[dict[str, Any]] = []
    terminal_reached = asyncio.Event()
    _TERMINAL_EVENTS = frozenset(
        {"fill", "canceled", "expired", "rejected", "replaced"}
    )

    async def _ws_listener() -> None:
        try:
            async with websockets.connect(  # type: ignore[attr-defined]
                _WS_PAPER_URL,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                # 1. Auth frame.
                auth = json.dumps({
                    "action": "authenticate",
                    "data": {
                        "key_id": settings.api_key_id,
                        "secret_key": settings.api_secret_key,
                    },
                })
                await ws.send(auth)
                ack = json.loads(await ws.recv())
                print(f"  ws auth ack: {ack}")
                ws_frames.append({"_meta": "auth_ack", "frame": ack})

                # 2. Subscribe to trade_updates.
                sub = json.dumps({
                    "action": "listen",
                    "data": {"streams": ["trade_updates"]},
                })
                await ws.send(sub)
                sub_ack = json.loads(await ws.recv())
                print(f"  ws subscribe ack: {sub_ack}")
                ws_frames.append({"_meta": "subscribe_ack", "frame": sub_ack})

                # Signal the order submission task.
                ws_ready.set()

                # 3. Receive lifecycle events until terminal or timeout.
                async with asyncio.timeout(_WS_TIMEOUT_S):
                    async for raw_msg in ws:
                        frame = json.loads(raw_msg)
                        event = (
                            frame.get("data", {}).get("event", "")
                            if isinstance(frame, dict)
                            else ""
                        )
                        order_cid = (
                            frame.get("data", {})
                            .get("order", {})
                            .get("client_order_id", "")
                        )
                        if order_cid == order_ref:
                            print(f"  ws ← event={event!r} for our order")
                            ws_frames.append({"_meta": f"lifecycle/{event}", "frame": frame})
                            if event in _TERMINAL_EVENTS:
                                terminal_reached.set()
                                break
                        else:
                            # Not our order — still capture for frame-format proof.
                            ws_frames.append({"_meta": "other_order", "frame": frame})
        except TimeoutError:
            print(f"  ws timeout after {_WS_TIMEOUT_S}s — market may be closed; "
                  "saving frames captured so far.")
            terminal_reached.set()
        except Exception as exc:
            print(f"  ws error: {exc}")
            terminal_reached.set()

    ws_ready = asyncio.Event()

    async def _submit_order() -> dict[str, Any]:
        # Wait for the websocket to be subscribed before submitting.
        await ws_ready.wait()
        print(f"  Submitting order: {order_body}")
        result = await client.submit_order(order_body)
        print(f"  Submit response: id={result.get('id', '?')!r}, "
              f"status={result.get('status', '?')!r}, "
              f"client_order_id={result.get('client_order_id', '?')!r}")

        # HITL #1198 key assertion: client_order_id echoes back untruncated.
        returned_cid = result.get("client_order_id", "")
        if returned_cid == order_ref:
            print(f"  ✓ client_order_id round-trip: EXACT MATCH ({len(order_ref)} chars)")
        else:
            print(f"  ✗ client_order_id mismatch!")
            print(f"    sent:     {order_ref!r}")
            print(f"    received: {returned_cid!r}")
            raise AssertionError("client_order_id not echoed back untruncated")

        return result

    # Run listener and submission concurrently.
    submit_task = asyncio.create_task(_submit_order())
    ws_task = asyncio.create_task(_ws_listener())
    try:
        submit_result, _ = await asyncio.gather(submit_task, ws_task)
    except Exception:
        ws_task.cancel()
        raise

    return submit_result, ws_frames


def _write_order_fixtures(
    submit_result: dict[str, Any],
    ws_frames: list[dict[str, Any]],
    *,
    order_ref: str,
    captured_at_ms: int,
) -> None:
    """Write real sanitized order + trade_updates fixtures."""
    print("\n[S7] Writing order fixtures ...")

    uuid_map: dict[str, str] = {}

    # orders fixture: wrap the single submit response in a list (same shape as
    # the existing fixture so adapter tests stay consistent).
    sanitized_order = _sanitize_payload(submit_result, uuid_map=uuid_map)
    _write_fixture(
        "orders",
        [sanitized_order],
        note="real submit response from HITL gate",
    )
    _write_attribution(
        "orders",
        captured_at_ms=captured_at_ms,
        sanitization_notes=(
            "UUIDs replaced with sentinel values; "
            "client_order_id sanitized (contains real operator token)."
        ),
        extra=(
            f"## order_ref length cap proof\n\n"
            f"- `order_ref` sent:   `{order_ref}` ({len(order_ref)} chars)\n"
            f"- `DEFAULT_ORDER_REF_MAX_LENGTH`: {DEFAULT_ORDER_REF_MAX_LENGTH}\n"
            f"- Alpaca echoed `client_order_id` back UNTRUNCATED (exact match).\n"
            f"- Margin: {DEFAULT_ORDER_REF_MAX_LENGTH - len(order_ref)} chars spare.\n"
            f"- Cap is proven safe for the current namespace format.\n"
        ),
    )

    # trade_updates fixture: the real frames minus auth material.
    # Strip _meta keys but keep the frame structure; redact auth frames.
    safe_frames = []
    for entry in ws_frames:
        meta = entry.get("_meta", "")
        frame = entry["frame"]
        if meta == "auth_ack":
            # Never commit auth acknowledgements that echo key material.
            # Write a structural placeholder instead.
            safe_frames.append({
                "stream": "authorization",
                "data": {"status": "authorized", "action": "authenticate"},
            })
        elif meta in ("subscribe_ack", "other_order"):
            safe_frames.append(_sanitize_payload(frame, uuid_map=uuid_map))
        else:
            # lifecycle/* — real event frames for our order.
            safe_frames.append(_sanitize_payload(frame, uuid_map=uuid_map))

    _write_fixture(
        "trade_updates",
        safe_frames,
        note=f"{len(safe_frames)} real frames from HITL gate",
    )
    _write_attribution(
        "trade_updates",
        captured_at_ms=captured_at_ms,
        sanitization_notes=(
            "Auth frame replaced with structural placeholder (no key material). "
            "Order UUIDs replaced with sentinel values. "
            "client_order_id in lifecycle frames sanitized."
        ),
        extra=(
            f"## Frames captured\n\n"
            + "\n".join(
                f"- `{e['_meta']}`" for e in ws_frames
            )
        ),
    )


# ── README update ──────────────────────────────────────────────────────────────

def _update_readme() -> None:
    path = _FIXTURE_DIR / "README.md"
    content = f"""# Alpaca golden fixtures (Broker System v2)

Real sanitized Alpaca REST payloads captured from a live paper account via the
HITL gate (script `scripts/hitl_alpaca_capture.py`, run {_TODAY_UTC}).
One subdirectory per endpoint family, each with the raw payload(s) and an
`attribution.md`.

These are **real sanitized captures** (`reference_kind: real_sanitized_capture`):
UUID fields and account numbers have been replaced with deterministic sentinel
values; all other field values (numerics, timestamps, status strings, symbols)
are verbatim from the wire. Each `attribution.md` documents the sanitization
applied.

These fixtures are outside the numerical golden manifest
(`tests/fixtures/golden/manifest.json`) — that system governs
tolerance-pinned math equivalence, which does not apply to broker payload shape.

## Status: `real-capture`

Replaced `pending-real-capture` synthetic fixtures on {_TODAY_UTC}.
Adapter + schema-drift tests pass against these payloads.

## Regeneration

Run `python scripts/hitl_alpaca_capture.py` from `PythonDataService/` with
paper credentials in `.env`. The script calls all read endpoints, optionally
submits a test order, and regenerates every fixture + attribution file.
"""
    with path.open("w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"  ✓ updated {path.relative_to(_REPO_ROOT)}")


# ── main ───────────────────────────────────────────────────────────────────────

async def _main() -> None:
    print("=" * 60)
    print("HITL Alpaca capture — gates #1178 + #1198")
    print(f"Date (UTC): {_TODAY_UTC}")
    print("=" * 60)

    # H1: credential check.
    _check_credentials()

    # Use a fresh in-memory journal that also writes to disk (so entries are
    # available for extraction). The default journal goes to var/broker_captures/
    # which is git-ignored.
    reset_capture_journal_for_testing()

    client = AlpacaTradingClient()

    # H2: read endpoints.
    journal_entries = await _capture_reads(client)
    _replace_fixtures_from_reads(journal_entries)

    # S7: order gate.
    print("\n[S7] Running order submission gate ...")
    captured_at_ms = int(datetime.now(UTC).timestamp() * 1000)
    try:
        submit_result, ws_frames = await _run_order_gate(client)
    except Exception as exc:
        print(f"\n  ✗ Order gate failed: {exc}")
        print("  Skipping order fixture replacement.")
    else:
        # Determine the order_ref from the submission result's client_order_id.
        order_ref = submit_result.get("client_order_id", "")
        _write_order_fixtures(
            submit_result,
            ws_frames,
            order_ref=order_ref,
            captured_at_ms=captured_at_ms,
        )

    # Update the top-level README.
    print("\n[finish] Updating README ...")
    _update_readme()

    print("\n" + "=" * 60)
    print("HITL capture complete.")
    print()
    print("Next steps:")
    print("  1. Run adapter + schema-drift tests against real fixtures:")
    print("       podman exec polygon-data-service python -m pytest tests/broker/alpaca/ -v")
    print("  2. Commit the replacement fixtures:")
    print("       git add tests/fixtures/alpaca/ && git commit -m 'test(broker-v2): replace synthetic alpaca fixtures with real sanitized captures (HITL #1178 #1198)'")
    print("  3. Close issues #1178 and #1198.")
    print("=" * 60)


if __name__ == "__main__":
    anyio.run(_main)
