# Lane A: Alpaca stream and REST resilience (#2079, #2082) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two independent PRs: split the trade_updates reconnect counter so backoff resets after a connected cycle without skipping gap reconciliation; retry an idempotent Alpaca GET once on a dead pooled connection, never a write.

**Architecture:** See the lane report below; every task carries its own files, failing test, implementation and commit.

**Tech Stack:** Python 3.12 / FastAPI / pytest (PythonDataService), Angular 22 / Vitest (Frontend), Podman Compose, GitHub Actions.

**Spec:** the GitHub issues named in the title, plus `docs/broker-clerk-fleet-authority.md` §6 (trust register). Master sequencing, owner decisions, deploy windows and the delivery protocol live in `2026-09-14-fleet-trust-register-fixes.md` — read it first; its Global Constraints apply to every task here.

## Global Constraints

Inherited verbatim from `2026-09-14-fleet-trust-register-fixes.md` § Global Constraints. In particular: never switch branches in `/Users/inkant/learn-ai` (the clerks bind-mount it); every `./restart.sh` drops both clerks; import-time changes need a no-bot window; run the thermo review once before the first push; fleet tests run unsandboxed.

## Corrections applied by the integrating session

- None.

---

*The report below was produced by an Opus planning agent on 2026-09-14 against origin/master `e2fdb23f` plus the branches of PR #2081 and PR #2078. Every file:line claim was verified by that agent at that time; re-verify after those PRs merge.*

---

I have everything I need. Here is the plan.

---

# Alpaca stream + REST resilience — plan for #2079 and #2082

Base: `origin/master` (e2fdb23f). `origin/fix/fleet-production-safety` (#2081) touches **no** file in this plan (`market_liveness.py`, `fleet_boot.py`, `agent_identity.py`, `main.py`, `restart.sh`, Frontend). Both changes here rebase cleanly onto it.

## 1. Verified facts

### Issue #2079 claims

- **CONFIRMED** `attempt = 0` before the loop — `PythonDataService/app/broker/alpaca/trade_updates.py:392`.
- **CONFIRMED** `attempt += 1` is the only mutation — `trade_updates.py:407`. `attempt` is never reset.
- **DRIFTED (off by one)** the issue cites `trade_updates.py:394` for `await self._consume_once(reconcile_after_connect=attempt > 0)`; it is at **`trade_updates.py:395`** (394 is the bare `try:`).
- **CONFIRMED** `attempt` is simultaneously the backoff input (`trade_updates.py:415` `await self._backoff(attempt)`), the reconnect-budget input (`trade_updates.py:409`), and the reconcile flag (`:395`).
- **CONFIRMED** the ceiling math. `_default_backoff` (`trade_updates.py:118-125`): `max_exponent = ceil(log2(30/1)) = 5`, so attempt 6 → `min(1*2^5, 30)` = **30 s**, and every lifetime error after the sixth costs a full 30 s.
- **CONFIRMED** production runs unbounded: `TradeUpdatesConsumer.for_alpaca` (`trade_updates.py:672…`) never passes `max_reconnects`, default `None` (`trade_updates.py:299`), wired at `app/main.py:619-625`.
- **PARTLY WRONG** — "Resetting it after a healthy cycle would make the next reconnect look like a first connect and skip gap reconciliation." With the #2081 sibling's *exact* shape (`if delivered: attempt = 0` **then** `attempt += 1`), `attempt` is ≥ 1 at the top of every cycle after the first, so `attempt > 0` stays True and reconciliation would **not** be skipped. The hazard is real only for the obvious variants — reset placed *after* the increment, or a `continue` that skips it. Treat the two-counter split as making the trap unrepresentable, and keep the negative test; do not repeat the issue's claim as fact in the PR body.
- **CONFIRMED, and decisive for the design** — `self._counters.reconnects` (`trade_updates.py:408`) increments in lockstep with `attempt`, so today `attempt == self._counters.reconnects` *within one `run()`*. It diverges across `stop()`/`start()` (the counter is on the instance, `attempt` is local). It also counts cycles that never connected.
- **CONFIRMED (the hazard the issue does not mention)** — if the reset is applied to the counter that feeds `_max_reconnects`, the budget silently becomes "consecutive failures" and **six existing tests hang forever**, because their frame-source factory yields the same frames on every cycle: `tests/broker/alpaca/test_trade_updates.py:279-286` (`_frame_source`) used at `:944`, `:1010`, `:1072`, `:1115`, `:1153` with `max_reconnects=1`, plus `tests/broker/alpaca/clerk/test_trade_evidence.py:663-675` (`_authorization_source`, `max_reconnects=1`). A hang, not a failure.
  - Same latent hazard now exists on #2081: `tests/broker/alpaca/test_market_liveness.py:530-584` terminates only because its third cycle raises `IndexError` off the end of the `attempts` list. Not my lane; worth a note on that PR.
- **CONFIRMED** no existing test records backoff arguments for trade_updates — `_no_backoff` (`test_trade_updates.py:349`) discards the value; `test_backoff_caps_before_exponentiation` (`:1179`) tests `_default_backoff` in isolation. The new assertion is genuinely new coverage.
- **CONFIRMED** "a cycle that delivered frames" has no existing counter. `_handle_frame` (`trade_updates.py:442`) increments one of several counters, and a control frame (`{"stream":"authorization"}`, `trade_updates.py:479-481`) increments **none**. The usable signal is `_mark_connection(True)` (`trade_updates.py:352-355`, called at `trade_updates.py:433`).
- **CONFIRMED** `_consume_once` semantics (`trade_updates.py:417-438`): returns immediately (no connect) when `anext` raises `StopAsyncIteration`; otherwise runs `_gap_reconcile()` **before** `_mark_connection(True)`; every other exception propagates to `run()`.
- **CONFIRMED** `_gap_reconcile` (`trade_updates.py:630-634`) calls `evidence_sink.reconcile_gap()` first, which for the live SQLite sink (`clerk/trade_evidence.py:304-307`) runs a full `reconcile_account(trigger="AUTOMATIC")` and **raises** on a `stale` verdict. So a persistently failing reconciler means the cycle never connects.
- **CONFIRMED** a 15 s periodic `ReconciliationSweep` (`clerk/sqlite/reconciliation_sweep.py:45,54`) independently reconciles the account, and it already uses the correct two-counter pattern (`reconciliation_sweep.py:356`: `consecutive_failures = 0 if succeeded else consecutive_failures + 1`). That is the in-repo precedent to cite, and it caps the blast radius of any reconcile the socket loop skips.

### Issue #2082 claims

- **CONFIRMED** `app/services/broker_account_snapshot.py:63` logs `broker account snapshot read failed`; **CONFIRMED** `app/routers/brokers.py:714` logs `Clerk status account read failed; reporting evidence-unavailable posture`. The pair is one failure: `get_clerk_status` → `resolve_broker_account_snapshot` → `port.get_account()`.
- **CONFIRMED** the client is **not** ours — it is alpaca-py's own `requests.Session`. `TradingClient` is built at `app/broker/alpaca/client.py:164-172`; we only decorate `client._session` with a timeout wrapper (`client.py:86-100`, installed `:171`) and a capture hook (`:173`).
- **CONFIRMED** that session is shared with writes. `alpaca/common/rest.py:69` `self._session = Session()`; every verb funnels through `_request` → `_one_request` → `self._session.request` (`rest.py:195`), and `get`/`post`/`delete` are `rest.py:212/227/268`. Our `submit_order` (`client.py:432`) and `cancel_order` (`client.py:458`) ride the same pool. **A session-wide retry is unsafe; method gating is the whole safety argument.**
- **CONFIRMED** alpaca-py does not cover this. Its retry loop catches only `RetryException` (`rest.py:129-134`), raised solely when `response.status_code in self._retry_codes` (`rest.py:201`), default `[429, 504]` (`alpaca/common/constants.py:13`). A `requests.exceptions.ConnectionError` out of `self._session.request` propagates un-retried.
- **CONFIRMED** installed versions in `PythonDataService/.venv`: python 3.12.13, **alpaca-py 0.42.0** (pinned, `requirements-light.txt:44`), **requests 2.34.2** (`requests>=2.28`, unpinned), **urllib3 1.26.20** (transitive, undeclared). The undeclared/unpinned urllib3 is itself an argument against a `urllib3.util.Retry`-shaped fix.
- **CONFIRMED** exception shapes (checked in the venv): `requests.exceptions.ConnectionError(ProtocolError("Connection aborted.", RemoteDisconnected(...)))` — `args[0]` is the `ProtocolError`; `ConnectTimeout` subclasses **both** `ConnectionError` and `Timeout`; `ReadTimeout` is **not** a `ConnectionError`.
- **CONFIRMED** `responses` can reproduce this deterministically: registering an exception body then a 200 for the same URL raises once, succeeds on the second call, and records 2 entries in `responses.calls` (ran it).
- **CONFIRMED** writes are not retried anywhere else: `_call_write` (`client.py:361-416`) retries only `BrokerRateLimited`; `submit_order`'s failure branch (`client.py:437-442`) issues no extra HTTP.
- **CONFIRMED** the read path never retries today: `_call` (`client.py:183-225`) maps `RequestException` → `BrokerUnavailable` on the first failure.

## 2. Decisions the owner must make

1. **Which counter answers "is this a reconnect?" — a monotonic lifetime cycle count, or a connect count.** Recommend the **monotonic cycle count** (`cycles > 0`, exactly today's `attempt > 0`), because it changes reconciliation triggering by zero and keeps the diff's only behavioural change in the backoff input.
2. **What resets the backoff — "a frame arrived" or "the cycle connected".** Recommend **connected** (`_counters.connects` increased), because `_gap_reconcile` runs between the first frame and the connection watermark, so a frame-keyed reset would let a permanently failing reconciler reset the backoff every cycle and hot-loop the socket at the 1 s floor.
3. **Whether to add a dwell-time floor so a socket that authenticates then immediately dies cannot reconnect every second forever.** Recommend **no, ship without it**, because #2081 shipped the same exposure for market-status and a second mechanism should be justified by logs rather than by speculation.
4. **Where the #2082 retry lives — a `urllib3.util.Retry` on the adapter, or an explicit wrapper on `Session.request`.** Recommend the **explicit wrapper**, because `Retry` offers no hook for the structured log line the issue explicitly requires, and urllib3 is unpinned and undeclared in our requirements.
5. **Retry budget for #2082 — 1 or 2.** Recommend **1**, because up to 8 connections can be pooled (`_MAX_IN_FLIGHT_SYNC_CALLS`, `client.py:64`) but the observed cadence is one stale socket per ~22 min; the new log line tells you if 1 is not enough.
6. **One PR or two.** Recommend **two** (see §4).

## 3. Task breakdown

### Task 1 — #2079: split the reconnect counters in `TradeUpdatesConsumer.run`

**Files**
- modify `PythonDataService/app/broker/alpaca/trade_updates.py`
- test `PythonDataService/tests/broker/alpaca/test_trade_updates.py`

**Regression test** (append to `tests/broker/alpaca/test_trade_updates.py`; needs no new imports — `asyncio`, `json`, `AsyncIterator`, `Path`, `pytest` are already imported at `:13-24`):

```python
class _ReconcileRecordingSink(_EvidenceSink):
    """An _EvidenceSink that records which loop cycle asked for a gap-fill."""

    def __init__(self, cycles: dict[str, int]) -> None:
        super().__init__()
        self._cycles = cycles
        self.reconciled_on_cycle: list[int] = []

    async def reconcile_gap(self) -> None:
        # ``_consume_once`` reconciles after ``anext`` has already advanced the
        # counter, so the cycle being reconciled is one behind it.
        self.reconciled_on_cycle.append(self._cycles["n"] - 1)


async def test_reconnect_backoff_resets_after_a_connected_cycle_and_still_gap_reconciles(
    tmp_path: Path,
) -> None:
    """A blip hours ago must not pin a healthy trade_updates stream at 30s.

    ``attempt`` fed ``_default_backoff`` *and* answered "is this a reconnect?".
    Set to zero once before the loop and only ever incremented, it pinned the
    backoff at its 30s ceiling after roughly six lifetime errors — and on this
    stream every second of backoff is a second the clerk is not receiving
    fills and order lifecycle events.

    Both halves of the split are pinned here. The backoff resets after a cycle
    that actually connected, AND the connect that follows the reset still runs
    the REST gap-reconcile: a single-counter reset that lands after the
    increment makes the next connect look like a first connect, skips the
    gap-fill, and silently drops the fills missed while down.
    """
    cycles = {"n": 0}
    backoff_attempts: list[int] = []
    broker = _FakeBroker()
    sink = _ReconcileRecordingSink(cycles)

    async def frame_source() -> AsyncIterator[bytes | str]:
        index = cycles["n"]
        cycles["n"] += 1
        if index >= 4:
            # End the loop deterministically; run() re-raises CancelledError
            # rather than swallowing it in its except-Exception.
            raise asyncio.CancelledError
        if index in (2, 3):
            # The connected cycles: each delivers a frame and ends cleanly.
            yield json.dumps({"stream": "authorization", "data": {"status": "authorized"}})
            return
        raise RuntimeError("simulated disconnect")

    async def _record_backoff(attempt: int) -> None:
        backoff_attempts.append(attempt)

    consumer = TradeUpdatesConsumer(
        evidence_sink=sink,
        read=broker,
        frame_source=frame_source,
        journal=_capture_journal(tmp_path),
        clock=lambda: _FIXED_MS,
        backoff=_record_backoff,
    )

    with pytest.raises(asyncio.CancelledError):
        await consumer.run()

    assert backoff_attempts == [1, 2, 1, 1], (
        "A connected cycle must reset the reconnect backoff. Without the reset "
        "this reads [1, 2, 3, 4] and keeps climbing to the 30s ceiling, where "
        "every later blip costs half a minute of unseen fills."
    )
    assert sink.reconciled_on_cycle == [2, 3], (
        "Every connect after the first cycle must REST gap-reconcile, "
        "including the one immediately after a backoff reset — that is the "
        "fill-losing regression a single-counter reset introduces."
    )
```

**Run (expect failure)**

```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/broker/alpaca/test_trade_updates.py::test_reconnect_backoff_resets_after_a_connected_cycle_and_still_gap_reconciles -q
```

Expected: `AssertionError: A connected cycle must reset the reconnect backoff...` / `assert [1, 2, 3, 4] == [1, 2, 1, 1]`. The second assertion passes before the fix — it is the guard, not the trigger.

**Implementation**

1. `trade_updates.py:142-151` — add a counter to `TradeUpdateCounters`: a docstring bullet ``- ``connects`` — cycles that reached the connection watermark.`` next to the existing `reconnects` bullet, and a field `connects: int = 0` immediately after `reconnects: int = 0`.
2. `trade_updates.py:352-355` — in `_mark_connection`, inside the `if self._connected != connected:` block and after the `_connection_changed_at_ms` assignment, add `if connected: self._counters.connects += 1`.
3. `trade_updates.py:384-415` — rewrite `run`'s loop body:

```python
        cycles = 0
        attempt = 0
        while True:
            connects_before = self._counters.connects
            try:
                await self._consume_once(reconcile_after_connect=cycles > 0)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A frame-source failure is surfaced, then retried under backoff
                # — never a silent death of the live lifecycle feed.
                logger.warning(
                    "alpaca trade_updates stream errored; will reconnect",
                    extra={"action": "trade_updates_stream_error"},
                    exc_info=True,
                )

            # "Healthy" is *connected*, not *a frame arrived*: ``_consume_once``
            # runs the gap-reconcile between the first frame and the connection
            # watermark, so keying the reset on the frame would let a
            # permanently failing reconciler reset the backoff every cycle and
            # hot-loop this socket at the 1s floor.
            if self._counters.connects > connects_before:
                attempt = 0
            attempt += 1
            cycles += 1
            self._counters.reconnects += 1
            if self._max_reconnects is not None and cycles > self._max_reconnects:
                logger.info(
                    "alpaca trade_updates reconnect budget exhausted; stopping",
                    extra={"action": "trade_updates_reconnect_budget_exhausted"},
                )
                return
            await self._backoff(attempt)
```

4. Extend `run`'s docstring (`trade_updates.py:384-391`) with a paragraph naming the split: `cycles` is monotonic and owns the reconnect budget **and** the "does this connect follow an earlier one" question; `attempt` is the backoff input only and resets after any cycle that connected. Name the sibling (`AlpacaMarketLivenessConsumer._consume_statuses`) and `ReconciliationSweep._run_forever` as the same pattern.

**Run (expect pass)**

```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/broker/alpaca/test_trade_updates.py tests/broker/alpaca/clerk/test_trade_evidence.py -q
ruff check PythonDataService/app/ PythonDataService/tests/
```

Expected: all pass, no new warnings. The `max_reconnects=1` tests must still terminate — if any hangs, the budget was wired to `attempt` instead of `cycles`.

**Commit**

```
fix(alpaca): reset trade_updates backoff on a connected cycle

The reconnect loop set `attempt = 0` once before the loop and only ever
incremented it, so after roughly six lifetime errors every reconnect paid
the full 30s ceiling. On trade_updates that is time the clerk is not
receiving fills or order lifecycle events.

`attempt` was also the "is this a reconnect?" flag that triggers the REST
gap-reconcile, so it could not simply be reset. Split it: `cycles` is
monotonic and owns both the reconnect budget and the gap-reconcile
question; `attempt` is the backoff input and resets after any cycle that
reached the connection watermark. Reset on *connected*, not on *a frame
arrived*, so a failing reconciler cannot hot-loop the socket.

Regression test pins both halves, including that the connect right after a
reset still gap-reconciles.
```

### Task 2 — #2082: retry an idempotent Alpaca read once on a dead pooled connection

**Files**
- modify `PythonDataService/app/broker/alpaca/client.py`
- test `PythonDataService/tests/broker/alpaca/test_client.py`

**Regression test** (append to `tests/broker/alpaca/test_client.py`; add imports `from pathlib import Path`, `import responses`, `from http.client import RemoteDisconnected`, `from urllib3.exceptions import ProtocolError`, `from requests.exceptions import ConnectionError as RequestsConnectionError`, `from app.broker.alpaca.client import _install_stale_connection_retry`, `from app.broker.alpaca.config import AlpacaSettings`, `from app.broker.capture.journal import CaptureJournal`, and module constants `_BASE = "https://paper-api.alpaca.markets"`, `_FIXED_MS = 1_700_000_000_000`):

```python
def test_stale_pooled_connection_retries_reads_but_never_writes() -> None:
    """Alpaca closes idle keep-alives; urllib3 hands the dead socket to the
    next request and it fails with RemoteDisconnected before the request was
    answered at all. Observed 16 times in 6 hours on the Live lane, each pair
    degrading the clerk's posture to evidence-unavailable.

    A GET is safe to re-issue. A POST through the SAME session is not — and
    alpaca-py gives order submission and cancellation that same session — so
    this test is the guarantee that the retry can never resubmit an order.
    """
    session = Session()
    attempts: list[str] = []

    def request(method: str, url: str, **kwargs: Any) -> str:
        attempts.append(method)
        if attempts.count(method) == 1:
            raise RequestsConnectionError(
                ProtocolError(
                    "Connection aborted.",
                    RemoteDisconnected("Remote end closed connection without response"),
                )
            )
        return "ok"

    session.request = request  # type: ignore[method-assign]
    _install_stale_connection_retry(session)

    assert session.get(f"{_BASE}/v2/account") == "ok"
    with pytest.raises(RequestsConnectionError):
        session.post(f"{_BASE}/v2/orders", json={"symbol": "SPY"})

    assert attempts == ["GET", "GET", "POST"]


@responses.activate
async def test_account_read_recovers_from_a_stale_pooled_connection(tmp_path: Path) -> None:
    """End-to-end over the real client the service builds, so the wiring in
    ``_build_default_client`` is covered and not just the helper."""
    responses.add(
        responses.GET,
        f"{_BASE}/v2/account",
        body=RequestsConnectionError(
            ProtocolError(
                "Connection aborted.",
                RemoteDisconnected("Remote end closed connection without response"),
            )
        ),
    )
    responses.add(
        responses.GET,
        f"{_BASE}/v2/account",
        json={"account_number": "PA1", "status": "ACTIVE"},
        status=200,
    )
    client = AlpacaTradingClient(
        settings=AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper"),
        journal=CaptureJournal(capture_dir=tmp_path / "capture", clock=lambda: _FIXED_MS),
    )

    payload = await client.get_account()

    assert payload["account_number"] == "PA1"
    assert len(responses.calls) == 2


@responses.activate
async def test_order_submission_is_not_retried_when_the_connection_drops(tmp_path: Path) -> None:
    """The write path shares the pool. One dropped connection must surface as
    one failed submission, never as a second order on the wire."""
    responses.add(
        responses.POST,
        f"{_BASE}/v2/orders",
        body=RequestsConnectionError(
            ProtocolError(
                "Connection aborted.",
                RemoteDisconnected("Remote end closed connection without response"),
            )
        ),
    )
    client = AlpacaTradingClient(
        settings=AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper"),
        journal=CaptureJournal(capture_dir=tmp_path / "capture", clock=lambda: _FIXED_MS),
    )

    with pytest.raises(BrokerUnavailable):
        await client.submit_order({"symbol": "SPY", "qty": "1", "side": "buy"})

    assert len(responses.calls) == 1
```

**Run (expect failure)**

```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/broker/alpaca/test_client.py -q -k stale_pooled or "stale_pooled_connection"
```

Expected before the fix: `ImportError: cannot import name '_install_stale_connection_retry' from 'app.broker.alpaca.client'` (collection error for the whole module). After stubbing the import away, the substantive failure is `test_account_read_recovers_from_a_stale_pooled_connection` raising `BrokerUnavailable: Could not reach Alpaca while fetching account.` with `len(responses.calls) == 1`.

**Implementation** — `app/broker/alpaca/client.py`

1. Imports: add `from urllib.parse import urlsplit`; change `from requests.exceptions import RequestException` to also import `ConnectionError as RequestsConnectionError` and `Timeout` (aliased so the builtin `ConnectionError` is never shadowed).
2. After `_install_session_timeout` (ends `client.py:100`), add the constant, helper and installer:

```python
# One retry, GET/HEAD only. Alpaca closes idle keep-alive connections and
# urllib3 hands the closed socket to the next request, which dies with
# RemoteDisconnected before a byte was answered — 16 occurrences in 6 hours on
# the Live lane, each degrading the clerk's account posture to
# evidence-unavailable. alpaca-py does not cover it: its own retry fires on
# HTTP status codes (429/504), never on a connection-level failure. The same
# requests.Session carries order submission (POST) and cancellation (DELETE),
# so this allowlist is the whole safety argument — a write is never re-issued.
_STALE_CONNECTION_RETRY_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})


def _connection_failure_kind(exc: BaseException) -> str:
    """The underlying urllib3/http.client error name, for the retry log line."""
    cause = exc.args[0] if exc.args else None
    return type(cause).__name__ if isinstance(cause, BaseException) else type(exc).__name__


def _install_stale_connection_retry(session: Session) -> None:
    """Re-issue an idempotent request once when the pooled connection was dead.

    Wraps ``Session.request`` rather than mounting a ``urllib3`` ``Retry``:
    ``Retry`` has no hook, and half the value of this fix is that the stale-pool
    recurrence stays visible instead of silently vanishing from the logs.
    ``Timeout`` is excluded — a connect timeout has already spent most of the
    caller's ``_DEFAULT_TIMEOUT_S`` budget and a read timeout may have reached
    the server, so neither is the "closed before any response" case.
    """
    request = session.request

    def request_with_retry(method: str, url: str, **kwargs: Any) -> Any:
        try:
            return request(method, url, **kwargs)
        except RequestsConnectionError as exc:
            if isinstance(exc, Timeout) or method.upper() not in _STALE_CONNECTION_RETRY_METHODS:
                raise
            logger.warning(
                "alpaca pooled connection was closed; re-issuing the idempotent request once",
                extra={
                    "action": "alpaca_stale_connection_retry",
                    "broker": BROKER_ID,
                    "method": method.upper(),
                    "path": urlsplit(url).path,
                    "cause": _connection_failure_kind(exc),
                },
            )
            return request(method, url, **kwargs)

    session.request = request_with_retry  # type: ignore[method-assign]
```

3. `client.py:171-173` — install it between the timeout wrapper and the capture hook, so the retry is the **outer** wrapper and each attempt still gets default timeouts:

```python
        _install_session_timeout(client._session, timeout_s=self._timeout_s)
        _install_stale_connection_retry(client._session)
        install_capture_hook(client._session, journal, broker=self.broker_id)
```

**Run (expect pass)**

```
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/broker/alpaca/test_client.py tests/broker/alpaca/test_capture_hook.py \
  tests/broker/alpaca/test_trade_updates.py -q
ruff check PythonDataService/app/ PythonDataService/tests/
```

**Commit**

```
fix(alpaca): retry an idempotent read once on a dead pooled connection

Alpaca closes idle keep-alive connections; urllib3 hands the closed socket
to the next request, which fails with RemoteDisconnected before the request
was answered. On the Live lane that surfaced 16 times in 6 hours as
"broker account snapshot read failed" plus an evidence-unavailable clerk
posture. alpaca-py's own retry fires on HTTP status codes (429/504) and
never on a connection-level failure.

Re-issue once, GET/HEAD only. alpaca-py gives order submission and
cancellation the same requests.Session, so the method allowlist is the
safety argument and is pinned by a test. The retried path logs
action=alpaca_stale_connection_retry so the recurrence stays measurable
instead of disappearing.
```

## 4. Grouping, sequencing, deployment

- **Two PRs.** Disjoint files, disjoint tests, disjoint failure modes. #2082 changes the HTTP session that carries order submission — it deserves its own review thread and its own revert handle, and must not be mixed with a websocket-loop change.
- **Fully parallel.** No shared file, no shared helper, no ordering constraint. Both rebase onto `origin/master` or onto #2081 without conflict.
- **Both are deploy-only-with-a-restart.** `restart.sh` runs `podman compose down` then `podman compose up -d --force-recreate` (`restart.sh:21,55`) — a full teardown of `polygon-data-service`, not a hot reload of `app/`. Deploying either change **kills the live `trade_updates` websocket and the clerk process**.
- **Unsafe while the Live lane carries a running bot.** Independently of these diffs: on restart the consumer is a fresh object, so `cycles` starts at 0 and the **first connect does not gap-reconcile** — today's behaviour, unchanged here, and the restart window's fills are recovered only by the 15 s `ReconciliationSweep`. Deploy flat and outside RTH, then confirm `trade_updates_authorized` in the logs before arming.
- **#2079 is the one to verify in production**, because its whole payoff is latency you can only see under real disconnects. After deploy, grep for `trade_updates_stream_error` and confirm the gaps between consecutive occurrences stop growing toward 30 s.

## 5. Risks the issue text missed

**#2079**

1. **The budget-semantics trap (highest-value finding).** Applying the sibling's reset to the counter that also feeds `_max_reconnects` converts the budget from "lifetime cycles" to "consecutive failures" and turns six existing tests into infinite loops (listed in §1). A hanging suite reads as CI flake, not as a bug. The `cycles`/`attempt` split is what avoids it.
2. **Resetting on "a frame arrived" creates a new hot loop.** `_gap_reconcile` runs before the connection watermark and raises on a `stale` reconciliation verdict. Key the reset on the frame and a broken reconciler resets the backoff every cycle → REST order-list pulls and full account reconciliations at the 1 s floor, against the same broker that is already failing.
3. **A flapping-but-authenticating socket now reconnects every second.** After the fix, any cycle that authenticates resets the backoff even if it dies 50 ms later. Alpaca enforces connection limits on `/stream`. Mitigation if logs show it: require the connected cycle to have lasted a minimum dwell time before resetting (decision 3).
4. **`self._counters.reconnects` is now the count of *attempts*, and `connects` the count of *successes*.** Anything that reads `reconnects` as "successful reconnects" is already wrong — `app/services/alpaca_sqlite_synthetic_frame_drills.py:561` asserts `reconnects == 2` and stays correct, but the new `connects` field is the one a health surface should prefer.
5. **Nothing reconciles the process-restart gap on first connect.** `cycles` starts at 0 in every new `run()`, so a cold start never gap-reconciles. The 15 s sweep covers it; if the sweep is ever disabled or fenced, this loop is not the backstop anyone assumes.

**#2082**

1. **The retry sits *inside* alpaca-py's own retry loop.** `_one_request` calls `self._session.request`, so on a 429/504 storm the worst case is alpaca-py's 4 attempts × our 2 = 8 HTTP requests, bounded only by `anyio.fail_after(15)` at `client.py:206`. Excluding `Timeout` from our retry keeps it from compounding, but state the multiplication in the PR.
2. **The 15 s call budget is shared.** A retry that lands after a slow failure can cross `_DEFAULT_TIMEOUT_S` and surface as `BrokerUnavailable("timed out")` instead of the connection error — a different message for the same root cause. Not worse, but it will confuse the next log reader.
3. **Up to 8 pooled connections can go stale at once** (`_MAX_IN_FLIGHT_SYNC_CALLS = 8`, `client.py:64`), so a single retry does not guarantee recovery after a long idle period. The log line is the instrument that tells you.
4. **The failed attempt leaves no capture record.** `install_capture_hook` fires on responses only, so the journal shows one ACCOUNT record per logical read. Nobody should read the journal as proof the retry did not happen — the log line is the only evidence.
5. **Every test that injects `client_factory` bypasses the fix.** `_real_broker_with_responses` (`tests/broker/alpaca/test_trade_updates.py:883-895`) builds a bare `TradingClient`, so it gets neither the timeout wrapper nor the retry. The new tests must go through `settings=`, not `client_factory=`.
6. **`requests.Session` is not thread-safe and we drive it from up to 8 anyio worker threads.** Pre-existing, untouched by this change, but the retry re-enters `Session.request` and makes the shared-pool assumption more load-bearing. Worth an explicit "unchanged, still true" note rather than silence.
7. **urllib3 is undeclared and unpinned** in `requirements-light.txt`/`requirements-heavy.txt` (it arrives transitively; the venv has 1.26.20). A `Retry`-based fix would silently change behaviour on a urllib3 2.x bump; the wrapper does not.

### Critical Files for Implementation

- `/Users/inkant/learn-ai/PythonDataService/app/broker/alpaca/trade_updates.py`
- `/Users/inkant/learn-ai/PythonDataService/tests/broker/alpaca/test_trade_updates.py`
- `/Users/inkant/learn-ai/PythonDataService/app/broker/alpaca/client.py`
- `/Users/inkant/learn-ai/PythonDataService/tests/broker/alpaca/test_client.py`
- `/Users/inkant/learn-ai/PythonDataService/app/broker/alpaca/market_liveness.py` (the #2081 sibling whose shape Task 1 follows)
