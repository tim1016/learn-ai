#!/usr/bin/env python3
"""Measure Alpaca fill-to-cash visibility for the live envelope's grace (#2487).

``FILL_VISIBILITY_GRACE_MS`` (``app/broker/alpaca/clerk/live_envelope.py``)
assumes a fill the Clerk recorded up to N ms before an account read was issued
is already reflected in that read's ``cash``. Alpaca publishes no ordering
guarantee between the ``trade_updates`` stream and the REST account view, so
the assumption is only as good as a measurement: this script records, for each
fill event received on the stream, how long afterwards an account read must be
*issued* before its ``cash`` reflects the fill. A read is dated when its
request is issued, never when it answers — the same discipline as
``LiveEnvelopeSync.observe``'s ``observed_at_ms`` (#2441), so the measured
delay is exactly the quantity the grace must cover.

Stdlib only and 3.9-compatible, mirroring ``scripts/alpaca_onboarding_gates.py``
(the operator holds broker credentials and must not have to install anything).
The stream speaks the raw wire protocol documented by the owned consumer
``app/broker/alpaca/trade_updates.py`` — authenticate, listen, JSON frames —
implemented over ``ssl``/``socket`` because the stdlib has no websocket client.

Subcommands
-----------
``observe``
    Read-only: subscribe to ``trade_updates``, poll ``GET /v2/account`` at
    ``--poll-ms``, and correlate every observed fill against the account-read
    series. Run this alongside normal account activity (paper bots, operator
    orders) on any endpoint.

``roundtrips``
    The paper-sandbox fill source: submits tiny market BUY/SELL round trips on
    a crypto pair (24/7, so a weekend can measure), waiting for each fill's
    cash visibility before the next leg, so exactly one fill is in flight and
    cash-delta attribution is unambiguous. Refuses on any endpoint other than
    ``paper-api.alpaca.markets`` — it places real (paper) orders.

Both write a JSON report (``--out``, default stdout) with per-fill records and
the delay distribution; every timestamp is int64 ms UTC. Exit 0 with the
report, 2 with a single ``REFUSED:``/``DISQUALIFIED:`` line on stderr when the
measurement could not run. Zero fills observed is a valid report (``observe``
on a quiet account), not a refusal.

Run directly::

    ALPACA_API_KEY_ID=... ALPACA_API_SECRET_KEY=... \\
    python3 scripts/measure_fill_to_cash_visibility.py roundtrips --count 12
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import urlparse

PAPER_ENDPOINT = "https://paper-api.alpaca.markets"

FILL_EVENTS = ("fill", "partial_fill")
# A read "reflects" a fill when its cash has moved at least this fraction of
# the fill's notional in the fill's direction. Below 1.0 to absorb regulatory
# fees on equity sells and the gap between a requested notional and the actual
# fill price; with one fill in flight at a time nothing else can contribute a
# same-direction cash move of that size.
REFLECTED_NOTIONAL_FRACTION = 0.9

# The socket read timeout must exceed the client keepalive interval: a quiet
# account delivers no frames for minutes, so the client pings to keep the
# stream alive and the timeout is what declares a dead stream.
KEEPALIVE_INTERVAL_S = 15.0
SOCKET_TIMEOUT_S = 60.0


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class CashReading(NamedTuple):
    """One account read, dated when its request was issued (#2441 discipline)."""

    issued_at_ms: int
    answered_at_ms: int
    cash: Optional[float]  # None when the read failed (kept for cadence audit)


class FillEvent(NamedTuple):
    """One fill/partial_fill event as received on ``trade_updates``."""

    receipt_at_ms: int  # wall clock when the frame finished arriving
    execution_id: str
    event_type: str  # "fill" | "partial_fill"
    symbol: str
    side: str  # "buy" | "sell"
    qty: float
    price: float
    order_id: str
    broker_at_ms: Optional[int]  # the event's own "timestamp", when parseable
    raw: Dict[str, Any]  # the event payload verbatim (no secrets ride this stream)

    @property
    def notional_usd(self) -> float:
        return self.qty * self.price


def parse_fill_event(data: Dict[str, Any], receipt_at_ms: int) -> Optional[FillEvent]:
    """One ``trade_updates`` event payload → a :class:`FillEvent`, or ``None``.

    ``None`` means "not a fill" (``new``/``canceled``/…) or a payload too
    malformed to attribute — both are skipped, never guessed at.
    """
    if data.get("event") not in FILL_EVENTS:
        return None
    order = data.get("order") or {}
    qty = _to_float(data.get("qty"))
    price = _to_float(data.get("price"))
    if qty is None or price is None or qty <= 0 or price <= 0 or not order.get("id"):
        return None
    return FillEvent(
        receipt_at_ms=receipt_at_ms,
        execution_id=str(data.get("execution_id") or ""),
        event_type=str(data["event"]),
        symbol=str(order.get("symbol") or ""),
        side=str(data.get("side") or order.get("side") or ""),
        qty=qty,
        price=price,
        order_id=str(order["id"]),
        broker_at_ms=parse_iso_ms(data.get("timestamp")),
        raw=data,
    )


def _to_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None  # NaN guard


def parse_iso_ms(value: Any) -> Optional[int]:
    """Alpaca's RFC3339 ``timestamp`` → int64 ms UTC, or ``None``.

    ``trade_updates`` timestamps carry nanosecond precision (9 fractional
    digits); ``datetime.fromisoformat`` on 3.9 accepts only 3 or 6, so the
    fraction is truncated to microseconds before parsing.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    head, dot, tail = text.partition(".")
    if dot:
        sign = ""
        for marker in ("+", "-"):
            offset = tail.find(marker, 1)
            if offset > 0:
                sign, tail = tail[offset:], tail[:offset]
                break
        tail = (tail + "000000")[:6]
        text = f"{head}.{tail}{sign}"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def baseline_cash(readings: List[CashReading], before_ms: int) -> Optional[float]:
    """The cash of the last read issued strictly before ``before_ms``.

    This is the fill's "before" world: with one fill in flight, any cash
    difference between it and a later read is that fill's effect.
    """
    for reading in reversed(readings):
        if reading.issued_at_ms < before_ms and reading.cash is not None:
            return reading.cash
    return None


def read_reflects_fill(cash: float, fill: FillEvent, before_cash: float) -> bool:
    threshold = before_cash + fill.notional_usd * REFLECTED_NOTIONAL_FRACTION * (
        -1.0 if fill.side == "buy" else 1.0
    )
    return cash <= threshold if fill.side == "buy" else cash >= threshold


def first_reflecting_read(
    fill: FillEvent,
    readings: List[CashReading],
    before_cash: float,
    *,
    from_ms: int,
) -> Optional[CashReading]:
    """The first read issued at/after ``from_ms`` whose cash reflects the fill.

    ``before_cash`` is the fill's pre-execution cash level — the caller
    anchors it to the order's submission (generated fills) or to the broker's
    execution timestamp (passive fills), never to the receipt alone: a broker
    that lands cash before the stream event leaves a pre-receipt read already
    post-fill, and a receipt-anchored baseline would then misread the fill as
    never becoming visible. ``from_ms`` is the same anchor; the scan may then
    find the transition before the receipt, which is the fill's cash *lead*
    over its own stream event.
    """
    for reading in readings:
        if reading.issued_at_ms < from_ms or reading.cash is None:
            continue
        if read_reflects_fill(reading.cash, fill, before_cash):
            return reading
    return None


def summarize(delays_ms: List[int]) -> Dict[str, Optional[int]]:
    """Nearest-rank delay distribution; empty input yields all-None fields."""
    if not delays_ms:
        return {
            "count": 0,
            "min_ms": None,
            "p50_ms": None,
            "p90_ms": None,
            "p95_ms": None,
            "max_ms": None,
        }
    ordered = sorted(delays_ms)

    def nearest_rank(fraction: float) -> int:
        return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]

    return {
        "count": len(ordered),
        "min_ms": ordered[0],
        "p50_ms": nearest_rank(0.50),
        "p90_ms": nearest_rank(0.90),
        "p95_ms": nearest_rank(0.95),
        "max_ms": ordered[-1],
    }


# --------------------------------------------------------------------------
# Raw websocket client for the trading stream. The protocol is the one the
# owned consumer speaks (trade_updates.py): JSON frames, authenticate, listen.
# --------------------------------------------------------------------------


class StreamRefused(Exception):
    """The stream could not be established or died before the run completed."""


class AlpacaHttpError(StreamRefused):
    """A REST call Alpaca answered with an HTTP error; ``body`` is parsed JSON."""

    def __init__(self, status: int, path: str, body: Dict[str, Any]) -> None:
        super().__init__(f"HTTP {status} on {path}: {json.dumps(body)[:400]}")
        self.status = status
        self.body = body


class _WsReader:
    """Frame reader over an established TLS socket: yields complete messages."""

    TEXT, BINARY, CLOSE, PING, PONG, CONTINUATION = 0x1, 0x2, 0x8, 0x9, 0xA, 0x0

    def __init__(self, sock: ssl.SSLSocket) -> None:
        self._sock = sock

    def _read_exact(self, count: int) -> bytes:
        chunks = []
        while count > 0:
            chunk = self._sock.recv(min(count, 65536))
            if not chunk:
                raise StreamRefused("stream closed mid-frame")
            chunks.append(chunk)
            count -= len(chunk)
        return b"".join(chunks)

    def read_message(self, send_pong: Callable[[bytes], None]) -> Optional[Tuple[int, bytes]]:
        """One complete message ``(opcode, payload)``; answers pings inline.

        Returns ``None`` on a server close frame. Fragmented messages are
        reassembled (Alpaca sends single text frames; this is belt-and-braces).
        """
        message_opcode = None
        payload = b""
        while True:
            head = self._read_exact(2)
            fin, opcode = bool(head[0] & 0x80), head[0] & 0x0F
            length = head[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            frame_payload = self._read_exact(length)
            if opcode == self.CLOSE:
                return None
            if opcode == self.PING:
                send_pong(frame_payload)
                continue
            if opcode in (self.TEXT, self.BINARY, self.CONTINUATION):
                if message_opcode is None:
                    message_opcode = opcode
                payload += frame_payload
                if fin:
                    return message_opcode, payload


class TradeUpdatesStream:
    """One authenticated ``trade_updates`` subscription (raw stdlib socket)."""

    def __init__(self, endpoint: str, key_id: str, secret_key: str) -> None:
        self._url = endpoint.replace("https://", "wss://").replace("http://", "ws://") + "/stream"
        self._key_id = key_id
        self._secret_key = secret_key
        self._sock: Optional[ssl.SSLSocket] = None
        self._send_lock = threading.Lock()
        self._keepalive: Optional[threading.Timer] = None

    # -- frame plumbing ----------------------------------------------------

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        # Client-to-server frames MUST be masked (RFC 6455 §5.1).
        if self._sock is None:
            raise StreamRefused("stream is not connected")
        mask = os.urandom(4)
        header = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        with self._send_lock:
            self._sock.sendall(header + mask + masked)

    def _send_json(self, message: Dict[str, Any]) -> None:
        self._send_frame(_WsReader.TEXT, json.dumps(message).encode())

    def send_pong(self, payload: bytes) -> None:
        self._send_frame(_WsReader.PONG, payload)

    def _schedule_keepalive(self) -> None:
        # A quiet account delivers no frames for minutes and the server idle
        # policy is unpublished, so the client pings on its own clock; the
        # reply (or lack of it inside SOCKET_TIMEOUT_S) is the liveness probe.
        self._keepalive = threading.Timer(
            KEEPALIVE_INTERVAL_S, self._keepalive_tick, ()
        )
        self._keepalive.daemon = True
        self._keepalive.start()

    def _keepalive_tick(self) -> None:
        try:
            self._send_frame(_WsReader.PING, b"")
        except OSError:
            return
        self._schedule_keepalive()

    # -- lifecycle ---------------------------------------------------------

    def connect(self, timeout_s: float = 15.0) -> None:
        parsed = urlparse(self._url)
        raw = socket.create_connection((parsed.hostname, parsed.port or 443), timeout=timeout_s)
        context = ssl.create_default_context()
        # A bare default context still admits TLSv1/1.1 negotiation on
        # Python 3.9; pin the floor. Alpaca's endpoints are TLS 1.2+.
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._sock = context.wrap_socket(raw, server_hostname=parsed.hostname)
        self._sock.settimeout(SOCKET_TIMEOUT_S)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {parsed.path or '/'} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise StreamRefused("handshake EOF")
            response += chunk
        status = response.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise StreamRefused(f"handshake refused: {status.decode(errors='replace').strip()}")
        self._send_json(
            {
                "action": "authenticate",
                "data": {"key_id": self._key_id, "secret_key": self._secret_key},
            }
        )
        reader = _WsReader(self._sock)
        while True:
            message = reader.read_message(self.send_pong)
            if message is None:
                raise StreamRefused("stream closed during authentication")
            frame = json.loads(message[1])
            if frame.get("stream") == "authorization":
                if frame.get("data", {}).get("status") != "authorized":
                    raise StreamRefused("authorization rejected (check credentials)")
                break
        self._send_json({"action": "listen", "data": {"streams": ["trade_updates"]}})
        self._schedule_keepalive()

    def reader(self) -> _WsReader:
        if self._sock is None:
            raise StreamRefused("stream is not connected")
        return _WsReader(self._sock)

    def close(self) -> None:
        if self._keepalive is not None:
            self._keepalive.cancel()
            self._keepalive = None
        if self._sock is not None:
            try:
                self._send_frame(_WsReader.CLOSE, b"")
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


# --------------------------------------------------------------------------
# REST (account reads, and order submission for the roundtrips fill source)
# --------------------------------------------------------------------------


class AlpacaRest:
    def __init__(self, endpoint: str, key_id: str, secret_key: str) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
            "Content-Type": "application/json",
        }

    def _request(self, path: str, method: str = "GET", body: Optional[Dict[str, Any]] = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self._endpoint + path, data=data, method=method, headers=self._headers
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")[:400]
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = {"message": raw}
            raise AlpacaHttpError(exc.code, path, parsed) from exc

    def read_account_cash(self) -> Tuple[CashReading, Dict[str, Any]]:
        """One account read, stamped at issue (never at answer)."""
        issued = now_ms()
        payload = self._request("/v2/account")
        return (
            CashReading(issued_at_ms=issued, answered_at_ms=now_ms(), cash=float(payload["cash"])),
            payload,
        )

    def asset(self, symbol: str) -> Dict[str, Any]:
        return self._request("/v2/assets/" + urllib.request.quote(symbol, safe=""))

    def submit_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("/v2/orders", "POST", order)

    def submit_sell(self, symbol: str, qty: str) -> Tuple[Dict[str, Any], Optional[str]]:
        """Submit a market sell, retrying once at the broker's stated balance.

        Paper crypto can credit a position a hair below the order's own
        ``filled_qty`` (observed 2026-09-27: requested 0.000175579 against
        0.00017514 available), and Alpaca answers that sell with HTTP 403
        code 40310000 naming the true balance in ``available``. The retry
        sells what is actually there; the note records the adjusted quantity.
        """
        order = {
            "symbol": symbol,
            "qty": qty,
            "side": "sell",
            "type": "market",
            "time_in_force": "gtc",
        }
        try:
            return self._request("/v2/orders", "POST", order), None
        except AlpacaHttpError as exc:
            available = exc.body.get("available")
            if exc.status != 403 or not available:
                raise
            return (
                self._request("/v2/orders", "POST", {**order, "qty": str(available)}),
                f"sell retried at broker-stated available qty {available}",
            )

    def get_order(self, order_id: str) -> Dict[str, Any]:
        return self._request("/v2/orders/" + order_id)


class AccountPoller(threading.Thread):
    """Fixed-cadence account reads into a locked list, until :meth:`stop`."""

    def __init__(self, rest: AlpacaRest, interval_ms: int) -> None:
        super().__init__(daemon=True)
        self._rest = rest
        self._interval_ms = interval_ms
        self._stopped = threading.Event()
        self.readings: List[CashReading] = []
        self._lock = threading.Lock()
        self.errors = 0

    def snapshot(self) -> List[CashReading]:
        with self._lock:
            return list(self.readings)

    def stop(self) -> None:
        self._stopped.set()

    def run(self) -> None:
        while not self._stopped.is_set():
            try:
                reading, _ = self._rest.read_account_cash()
            except (StreamRefused, OSError, ValueError, KeyError):
                reading = CashReading(
                    issued_at_ms=now_ms(), answered_at_ms=now_ms(), cash=None
                )
                self.errors += 1
            with self._lock:
                self.readings.append(reading)
            self._stopped.wait(
                max(0.0, (self._interval_ms - (now_ms() - reading.issued_at_ms)) / 1000.0)
            )


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


class FillSink:
    """The stream thread's handoff point: pending events + lifecycle state."""

    def __init__(self) -> None:
        self.closed = threading.Event()
        self.close_reason: Optional[str] = None
        self._lock = threading.Lock()
        self.events: List[Tuple[Dict[str, Any], int]] = []

    def push(self, data: Dict[str, Any], receipt_at_ms: int) -> None:
        with self._lock:
            self.events.append((data, receipt_at_ms))

    def drain(self) -> List[Tuple[Dict[str, Any], int]]:
        with self._lock:
            out, self.events = self.events, []
        return out

    def close(self, reason: str) -> None:
        self.close_reason = reason
        self.closed.set()

    def fail_fast(self) -> None:
        if self.closed.is_set():
            raise StreamRefused(self.close_reason or "stream closed")


def _stream_thread(stream: TradeUpdatesStream, sink: FillSink) -> None:
    reader = stream.reader()
    try:
        while not sink.closed.is_set():
            message = reader.read_message(stream.send_pong)
            if message is None:
                sink.close("stream closed by server")
                return
            receipt = now_ms()
            try:
                frame = json.loads(message[1])
            except ValueError:
                continue
            if frame.get("stream") != "trade_updates":
                continue
            sink.push(frame.get("data") or {}, receipt)
    except (StreamRefused, OSError, ssl.SSLError, socket.timeout) as exc:
        sink.close(f"stream error: {exc}")


def _wait_for(condition: Callable[[], Any], timeout_s: float) -> Optional[Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = condition()
        if result is not None:
            return result
        time.sleep(0.05)
    return None


def _await_order_terminal(
    rest: AlpacaRest, order_id: str, timeout_s: float
) -> Optional[Dict[str, Any]]:
    """Poll the order until a terminal state (sizing only; never for timing).

    The stream remains the only receipt-time source; this REST poll exists so
    a notional buy that fills in parts is sold back at its true cumulative
    ``filled_qty`` instead of the first partial event's quantity.
    """
    deadline = time.monotonic() + timeout_s

    def terminal() -> Optional[Dict[str, Any]]:
        order = rest.get_order(order_id)
        return order if order.get("status") in ("filled", "canceled", "expired", "rejected") else None

    while time.monotonic() < deadline:
        order = terminal()
        if order is not None:
            return order
        time.sleep(0.3)
    return None


def _await_order_fill_events(
    sink: FillSink, order_id: str, timeout_s: float
) -> List[FillEvent]:
    """Every fill event the stream delivered for ``order_id`` within the window.

    Non-matching events are preserved for later correlation (a completing
    ``fill`` can trail the ``partial_fill`` the caller already consumed).
    """
    pending: List[Tuple[Dict[str, Any], int]] = []
    matches: List[FillEvent] = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for data, receipt in sink.drain():
            event = parse_fill_event(data, receipt)
            if event is not None and event.order_id == order_id:
                matches.append(event)
            else:
                pending.append((data, receipt))
        if matches:
            break
        time.sleep(0.05)
    for data, receipt in pending:
        event = parse_fill_event(data, receipt)
        if event is not None and event.order_id == order_id:
            matches.append(event)
    return matches


def _record_fill(
    fill: FillEvent,
    poller: AccountPoller,
    visibility_timeout_s: float,
    baseline_before_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """Correlate one fill against the read series; waits for visibility.

    ``baseline_before_ms`` anchors the pre-fill cash to the last read issued
    before that instant. Generated fills pass their order's submission time —
    strictly before execution, so the baseline can never already include the
    fill even when the broker lands the cash *before* the stream event
    arrives (the dominant observation on paper crypto, 2026-09-27). Without
    it the pre-receipt read may already be post-fill, and a fill visible at
    receipt would be misread as never becoming visible.

    Three verdicts: ``visible_before_receipt`` (the cash effect was already
    readable before the stream event arrived — the safe direction for the
    envelope, since a read issued at the receipt instant already reflects the
    fill), ``resolved`` (first reflecting read issued at/after the receipt —
    an upper bound quantized by the poll cadence), and
    ``not_visible_within_window`` (no reflecting read inside the window — the
    dangerous direction; the fill's visibility delay exceeds the window).
    """
    readings = poller.snapshot()
    if baseline_before_ms is not None:
        before_cash = baseline_cash(readings, baseline_before_ms)
        baseline_source = "order-submission"
        anchor_ms = baseline_before_ms
    else:
        # Passive mode: the broker's own execution timestamp is the only
        # pre-fill anchor, and it is a cross-clock comparison.
        anchor_ms = min(fill.broker_at_ms, fill.receipt_at_ms) if (
            fill.broker_at_ms is not None
        ) else fill.receipt_at_ms
        before_cash = baseline_cash(readings, anchor_ms)
        baseline_source = "broker-timestamp-or-receipt"

    def find() -> Optional[CashReading]:
        if before_cash is None:
            return None
        return first_reflecting_read(
            fill, poller.snapshot(), before_cash, from_ms=anchor_ms
        )

    record: Dict[str, Any] = {
        "execution_id": fill.execution_id,
        "event_type": fill.event_type,
        "symbol": fill.symbol,
        "side": fill.side,
        "qty": fill.qty,
        "price": fill.price,
        "notional_usd": fill.notional_usd,
        "order_id": fill.order_id,
        "receipt_at_ms": fill.receipt_at_ms,
        "broker_at_ms": fill.broker_at_ms,
        "fill_source": "websocket",
        "baseline_source": baseline_source,
        "baseline_cash_usd": before_cash,
        "raw_event": fill.raw,
    }
    reading = find()
    if reading is None and before_cash is not None:
        reading = _wait_for(find, visibility_timeout_s)
    if reading is None:
        record["status"] = "no_baseline" if before_cash is None else "not_visible_within_window"
        if record["status"] == "not_visible_within_window":
            record["window_s"] = visibility_timeout_s
        return record
    record["first_reflecting_read_issued_at_ms"] = reading.issued_at_ms
    record["first_reflecting_read_answered_at_ms"] = reading.answered_at_ms
    record["first_reflecting_read_cash_usd"] = reading.cash
    if reading.issued_at_ms < fill.receipt_at_ms:
        record["status"] = "visible_before_receipt"
        record["cash_led_receipt_by_ms"] = fill.receipt_at_ms - reading.issued_at_ms
    else:
        record["status"] = "resolved"
        record["visibility_delay_ms"] = reading.issued_at_ms - fill.receipt_at_ms
        record["visibility_delay_answer_ms"] = reading.answered_at_ms - fill.receipt_at_ms
    return record


def _floor_to_increment(value: float, increment: float) -> float:
    return round(int(value / increment + 1e-9) * increment, 10)


def _await_settled_reads(poller: AccountPoller, equal_reads: int, timeout_s: float) -> bool:
    """Wait until ``equal_reads`` consecutive reads agree on the cash level.

    Without this, one round's sell credit and the next round's buy debit can
    land inside a single read gap and net out, making neither leg's cash step
    individually observable (the two censored fills of the 2026-09-27
    rehearsal run). Settling between legs gives each fill's transition its
    own gap. Returns False on timeout — the caller proceeds, and any affected
    fill is then honestly censored.
    """
    deadline = time.monotonic() + timeout_s
    stable = 0
    last: Optional[float] = None
    while time.monotonic() < deadline:
        snapshot = poller.snapshot()
        if snapshot:
            cash = snapshot[-1].cash
            if cash is not None and cash == last:
                stable += 1
                if stable >= equal_reads:
                    return True
            else:
                stable = 1 if cash is not None else 0
            last = cash
        time.sleep(0.1)
    return False


def _run_one_roundtrip(
    rest: AlpacaRest,
    sink: FillSink,
    poller: AccountPoller,
    index: int,
    symbol: str,
    notional_usd: float,
    increment: float,
    min_order_size: float,
    visibility_timeout_s: float,
    poll_ms: int,
    skipped: List[str],
) -> List[Dict[str, Any]]:
    """One BUY/SELL round trip; returns the fill records it produced.

    Sequential by construction: the sell is sized from the buy's terminal
    state and submitted only after the buy's fill visibility resolved (or
    censored), so at most one fill is in flight and the cash-delta
    attribution to each fill is unambiguous.
    """
    fills: List[Dict[str, Any]] = []
    # Start from an observed, settled cash level so the first baseline is real.
    _await_settled_reads(poller, equal_reads=2, timeout_s=10.0)
    buy_submitted_ms = now_ms()
    buy = rest.submit_order(
        {
            "symbol": symbol,
            "notional": f"{notional_usd:.2f}",
            "side": "buy",
            "type": "market",
            "time_in_force": "gtc",
        }
    )
    buy_events = _await_order_fill_events(sink, buy["id"], visibility_timeout_s)
    buy_terminal = _await_order_terminal(rest, buy["id"], visibility_timeout_s)
    for event in buy_events:
        fills.append(_record_fill(event, poller, visibility_timeout_s, buy_submitted_ms))
    if not buy_events or buy_terminal is None:
        skipped.append(
            f"round {index}: buy {buy['id']} status "
            f"{(buy_terminal or {}).get('status', 'unknown')}, "
            f"{len(buy_events)} fill events"
        )
        return fills
    filled_qty = float(buy_terminal["filled_qty"])
    sell_qty = _floor_to_increment(filled_qty, increment) if increment else filled_qty
    if sell_qty <= 0 or sell_qty < min_order_size:
        skipped.append(
            f"round {index}: filled qty {filled_qty} not sellable "
            f"(min {min_order_size}, increment {increment})"
        )
        return fills
    sell_submitted_ms = now_ms()
    sell, sell_note = rest.submit_sell(symbol, f"{sell_qty:.10f}".rstrip("0").rstrip("."))
    if sell_note:
        skipped.append(f"round {index}: {sell_note}")
    sell_events = _await_order_fill_events(sink, sell["id"], visibility_timeout_s)
    sell_terminal = _await_order_terminal(rest, sell["id"], visibility_timeout_s)
    for event in sell_events:
        fills.append(_record_fill(event, poller, visibility_timeout_s, sell_submitted_ms))
    if not sell_events or sell_terminal is None:
        skipped.append(
            f"round {index}: sell {sell['id']} status "
            f"{(sell_terminal or {}).get('status', 'unknown')}, "
            f"{len(sell_events)} fill events"
        )
    # The sell's cash credit can land after its event; settle before the next
    # round so its transition never shares a read gap with the next buy's
    # debit (which would net out and censor both — the 2026-09-27 artifact).
    _await_settled_reads(poller, equal_reads=2, timeout_s=10.0)
    return fills


def run_roundtrips(
    rest: AlpacaRest,
    stream: TradeUpdatesStream,
    *,
    count: int,
    symbol: str,
    notional_usd: float,
    poll_ms: int,
    visibility_timeout_s: float,
) -> Dict[str, Any]:
    asset = rest.asset(symbol)
    # Crypto assets declare ``min_trade_increment``; equities declare
    # ``size_increment``. Either floors the sell leg below the filled quantity
    # without tripping the broker's precision validation.
    increment = float(asset.get("min_trade_increment") or asset.get("size_increment") or 0.0)
    min_order_size = float(asset.get("min_order_size") or 0.0)
    sink = FillSink()
    threading.Thread(target=_stream_thread, args=(stream, sink), daemon=True).start()
    poller = AccountPoller(rest, poll_ms)
    poller.start()
    baseline_reading, baseline_account = rest.read_account_cash()
    fills: List[Dict[str, Any]] = []
    skipped: List[str] = []
    try:
        for index in range(count):
            sink.fail_fast()
            try:
                fills.extend(
                    _run_one_roundtrip(
                        rest, sink, poller, index, symbol, notional_usd, increment,
                        min_order_size, visibility_timeout_s, poll_ms, skipped,
                    )
                )
            except AlpacaHttpError as exc:
                # One broker-rejected round must not discard the rounds
                # already measured; a dead stream still fails the run.
                skipped.append(f"round {index}: {exc}")
    finally:
        poller.stop()
        poller.join(timeout=5)
    final_reading, _ = rest.read_account_cash()
    return _report(
        mode="generated-roundtrips",
        symbol=symbol,
        poll_ms=poll_ms,
        baseline=baseline_reading,
        final=final_reading,
        account_number=baseline_account["account_number"],
        fills=fills,
        notes={
            "notional_usd": notional_usd,
            "skipped_legs": skipped,
            "read_errors": poller.errors,
        },
        readings=poller.snapshot(),
    )


def run_observe(
    rest: AlpacaRest,
    stream: TradeUpdatesStream,
    *,
    duration_s: float,
    poll_ms: int,
    visibility_timeout_s: float,
) -> Dict[str, Any]:
    sink = FillSink()
    threading.Thread(target=_stream_thread, args=(stream, sink), daemon=True).start()
    poller = AccountPoller(rest, poll_ms)
    poller.start()
    baseline_reading, baseline_account = rest.read_account_cash()
    fills: List[Dict[str, Any]] = []
    deadline = time.monotonic() + duration_s
    try:
        while time.monotonic() < deadline:
            sink.fail_fast()
            for data, receipt in sink.drain():
                event = parse_fill_event(data, receipt)
                if event is not None:
                    fills.append(_record_fill(event, poller, visibility_timeout_s))
            time.sleep(0.25)
    finally:
        poller.stop()
        poller.join(timeout=5)
    final_reading, _ = rest.read_account_cash()
    return _report(
        mode="passive-observe",
        symbol=None,
        poll_ms=poll_ms,
        baseline=baseline_reading,
        final=final_reading,
        account_number=baseline_account["account_number"],
        fills=fills,
        notes={"read_errors": poller.errors},
        readings=poller.snapshot(),
    )


def _report(
    *,
    mode: str,
    symbol: Optional[str],
    poll_ms: int,
    baseline: CashReading,
    final: CashReading,
    account_number: str,
    fills: List[Dict[str, Any]],
    notes: Dict[str, Any],
    readings: Optional[List[CashReading]] = None,
) -> Dict[str, Any]:
    # The distribution answers the envelope's question per fill: how long
    # after the Clerk could have recorded the fill (its stream receipt) must
    # an account read be issued before it reflects the fill? A fill whose
    # cash landed before the event is already reflected at the receipt
    # instant — delay 0. Only genuinely unresolved fills are censored.
    measured = [record for record in fills if record["status"] in ("resolved", "visible_before_receipt")]
    censored = [record for record in fills if record["status"] not in ("resolved", "visible_before_receipt")]
    delays = [
        record.get("visibility_delay_ms", 0) for record in measured
    ]
    report: Dict[str, Any] = {
        "schema": "fill-visibility-measurement/1",
        "mode": mode,
        "symbol": symbol,
        "account_number": account_number,
        "poll_interval_ms": poll_ms,
        "started_at_ms": baseline.issued_at_ms,
        "ended_at_ms": final.answered_at_ms,
        "cash_before_usd": baseline.cash,
        "cash_after_usd": final.cash,
        "fills": fills,
        "censored_fills": censored,
        "distribution": summarize(delays),
        "notes": notes,
    }
    if readings is not None:
        # The full read series makes the report self-auditable: every delay
        # above can be recomputed from it plus the fill records alone.
        intervals = [
            later.issued_at_ms - earlier.issued_at_ms
            for earlier, later in zip(readings, readings[1:])
        ]
        report["read_series"] = [
            {
                "issued_at_ms": reading.issued_at_ms,
                "answered_at_ms": reading.answered_at_ms,
                "cash_usd": reading.cash,
            }
            for reading in readings
        ]
        report["read_cadence"] = summarize(intervals)
    return report


def _print_summary(report: Dict[str, Any]) -> None:
    distribution = report["distribution"]
    print(
        "fills={count} min={min_ms}ms p50={p50_ms}ms p90={p90_ms}ms p95={p95_ms}ms "
        "max={max_ms}ms censored={censored} poll={poll}ms mode={mode}".format(
            censored=len(report["censored_fills"]),
            poll=report["poll_interval_ms"],
            mode=report["mode"],
            **distribution,
        )
    )


def main(argv: List[str]) -> int:
    # Shared options live on a parent parser so they parse both before and
    # after the subcommand name (``--poll-ms 200 roundtrips`` and
    # ``roundtrips --poll-ms 200`` both work).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--endpoint", default=PAPER_ENDPOINT, help="Alpaca trading endpoint (default paper)"
    )
    common.add_argument("--poll-ms", type=int, default=200, help="account read cadence")
    common.add_argument(
        "--visibility-timeout-s", type=float, default=30.0, help="per-fill wait before censoring"
    )
    common.add_argument("--out", default=None, help="write the JSON report to this path")
    parser = argparse.ArgumentParser(parents=[common], description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "observe", parents=[common], help="read-only: measure fills from normal account activity"
    ).add_argument("--duration-s", type=float, default=3600.0)
    roundtrips = sub.add_parser(
        "roundtrips",
        parents=[common],
        help="paper only: generate tiny crypto round trips as the fill source",
    )
    roundtrips.add_argument("--count", type=int, default=12)
    roundtrips.add_argument("--symbol", default="BTC/USD")
    roundtrips.add_argument("--notional", type=float, default=15.0)
    args = parser.parse_args(argv)

    key_id = os.environ.get("ALPACA_API_KEY_ID", "")
    secret_key = os.environ.get("ALPACA_API_SECRET_KEY", "")
    if not key_id or not secret_key:
        print("REFUSED: ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY not set", file=sys.stderr)
        return 2
    if args.command == "roundtrips" and args.endpoint.rstrip("/") != PAPER_ENDPOINT:
        print(
            f"DISQUALIFIED: roundtrips places orders and is paper-only (endpoint {args.endpoint})",
            file=sys.stderr,
        )
        return 2

    rest = AlpacaRest(args.endpoint, key_id, secret_key)
    stream = TradeUpdatesStream(args.endpoint, key_id, secret_key)
    try:
        stream.connect()
        if args.command == "roundtrips":
            report = run_roundtrips(
                rest,
                stream,
                count=args.count,
                symbol=args.symbol,
                notional_usd=args.notional,
                poll_ms=args.poll_ms,
                visibility_timeout_s=args.visibility_timeout_s,
            )
        else:
            report = run_observe(
                rest,
                stream,
                duration_s=args.duration_s,
                poll_ms=args.poll_ms,
                visibility_timeout_s=args.visibility_timeout_s,
            )
    except (StreamRefused, OSError, KeyError, ValueError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    finally:
        stream.close()
    payload = json.dumps(report, indent=2)
    if args.out:
        with open(args.out, "w") as handle:
            handle.write(payload + "\n")
    else:
        print(payload)
    _print_summary(report)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
