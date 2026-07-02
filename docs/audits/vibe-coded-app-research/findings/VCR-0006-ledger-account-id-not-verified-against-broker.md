---
id: VCR-0006
severity: P1
status: remediated
area: run-ledger
canonical_file: PythonDataService/app/engine/live/live_engine.py:360
reference: docs/architecture/adrs/0006-deploy-control-plane-host-daemon-init-ledger.md
first_seen: 2026-06-14
last_seen: 2026-06-14
remediation_progress:
  - "#495 — Phase 3 — strict ledger↔broker account identity (start-time)"
  - "PR feat/phase-3-reconnect-validation — Phase 3 reconnect re-validation: LiveEngine._check_reconnect_revalidation snapshots client.connectivity_lost_count between bars; on increment + restored connection re-runs verify_account_match; mismatch → halt.flag + ReconnectAccountMismatchHaltError; match → connection_epoch bumps for the failure list"
follow_up_required:
  - "Persist connection_epoch into session_metadata.json on each reconnect (currently held only on the engine instance — survives a future _write_session_metadata_on_start once that path is extended)"
  - "BROKER_RECONNECTED / RECONNECT_ACCOUNT_MISMATCH_HALT WAL events — gated on broker-lifecycle WAL location decision (intent_wal extension vs new broker_wal); halt.flag + the exception carry the same forensic payload until then"
lens: run-ledger-identity-provenance
dedupe_with_F: none
confidence: high
---

## Remediation (#495 / Phase 3) — start-time gate

Closed at the **start path** by issue #495. The reconnect re-validation
path (PRD §11 C) is captured as a follow-up because hooking the IBKR
client's auto-reconnect lifecycle requires non-trivial changes to
``IbkrClient`` that warrant a focused PR; the start-time gate already
eliminates the primary risk (misconfigured-deploy → wrong-account fills).

What ships:

- ``app/engine/live/account_identity.py`` — pure helpers and typed
  errors:
  - ``normalize_account_id`` — ``raw.strip().upper()`` + regex
    ``^[A-Z][A-Z0-9]+$``. Raises ``InvalidAccountIdError`` on empty /
    internal whitespace / non-alphanumeric / leading-digit.
  - ``verify_account_match`` — refuses to proceed unless the normalized
    ledger and broker accounts agree. Raises
    ``AccountIdentityMismatchError`` carrying both raw values so the
    operator's failure list surfaces deploy-time and runtime-bound
    identities side by side.
- ``app/engine/live/live_engine.py`` — ``_validate_paper_client`` calls
  ``verify_account_match`` AFTER the existing paper-sentinel checks but
  BEFORE strategy initialization (PRD §11 B).
- ``app/engine/live/session_metadata.py`` — ``SessionMetadata`` +
  atomic writer / reader. ``LiveEngine`` writes
  ``artifacts/live_runs/<run_id>/session_metadata.json`` after the
  identity gate passes; ``connection_epoch`` increments on each
  subsequent successful session in the same ``run_dir``.
- ``app/engine/live/run.py`` — ``cmd_start`` catches the two new typed
  errors AHEAD of the generic ``except Exception``. Exits 1 with
  ``ExitReason.fatal_halt`` and emits no recovery flatten (touching
  orders on the wrong account is exactly what the gate prevents).

Regression tests:

- ``tests/engine/live/test_account_identity.py`` — 13 tests covering
  normalization (case, whitespace, internal whitespace, non-alphanumeric,
  empty, leading digit), comparison (match, mismatch, no prefix shortcut,
  malformed either side), and ``LiveEngine`` integration (refuses to
  start on mismatch, writes session_metadata on match, increments
  connection_epoch).
- ``tests/engine/live/test_session_metadata.py`` — round-trip, overwrite,
  missing-file, corrupt-file, raw-values-persisted.

## Follow-up (deferred to a separate issue)

The reconnect re-check (PRD §11 C) requires:

- A connection-epoch counter on ``IbkrClient`` that increments on each
  successful (re)connect.
- A polling hook in ``LiveEngine``'s bar loop (or a callback wired
  through the error-event handler at codes 1101/1102) that calls
  ``verify_account_match`` against the broker's currently-bound account.
- A new ``RECONNECT_ACCOUNT_MISMATCH_HALT`` WAL event (extends
  ``IntentEventType``) and a ``BROKER_RECONNECTED`` event on a clean
  rebind.
- A durable ``desired_state = PAUSED`` write on mismatch, paired with
  ``halt.flag``.

These pieces depend on the broader ADR-0008 / Phase 5 wiring landing
first (intent WAL + ownership query). The start-time gate, which is
where the original $250k-class drift is materially blocked, is the
high-value half and ships now.

---

## What

The run ledger hashes `account_id` into `run_id` as one of seven identity fields (`compute_run_id`). At deploy time the operator supplies the string verbatim via `HostRunnerDeployRequest.account_id` (min-length=1, no DU-prefix check). At start time, `cmd_start` passes `ledger.account_id` into `LiveEngine` and into the `[START] account=…` console line. `LiveEngine._validate_paper_client` only checks that the broker's `connected_account` (returned by the IBKR Gateway) is a `DU*` paper account and that the mode/port are paper. It never compares `connected_account` against `ledger.account_id`.

If the operator's IBKR settings (env vars, Gateway client_id, account selection) bind to a different `DU*` paper account than they typed at deploy, the engine starts happily. Orders go to the wrong account. The executions parquet row stamps `account_id = ledger.account_id` (operator-typed, not broker-bound). The Provenance card UI confidently shows the operator-typed account while a different paper account is actually being filled.

`cmd_emergency_flatten` *does* compare `connected_account` to the operator-supplied `--account` arg as a defense-in-depth gate (the pattern exists in this codebase). It was not applied to the start path.

## Where

- `PythonDataService/app/engine/live/live_engine.py:360-370` — `_validate_paper_client` checks `settings.mode == "paper"` + DU prefix; never compares `self._account_id` against `self._client.connected_account`.
- `PythonDataService/app/engine/live/live_engine.py:309,909` — `self._account_id = ledger.account_id` (operator-typed) is what gets stamped into executions parquet.
- `PythonDataService/app/engine/live/run.py:1098-1110` — `cmd_start` passes `account_id=ledger.account_id` to `LiveEngine`.
- `PythonDataService/app/engine/live/run.py:1143` — `[START] run_id=… account={ledger.account_id}` — both operator-typed.
- `PythonDataService/app/broker/ibkr/client.py:374` — `self._connected_account` is whatever the IBKR Gateway returns (`managedAccounts`).
- `PythonDataService/app/engine/live/run.py::cmd_emergency_flatten` — demonstrates the comparison pattern (proves the team knows it should exist).

## Why this severity

PRD §7 P1: "architectural / SoT drift that causes incorrect operator decisions". The identity layer's whole job is to attest "this run is on this account, traceable to this code SHA, this audit copy, etc." The engine actively breaks the attestation at start time — every artifact (executions parquet, reconciliation, Provenance card, sizing card) carries the wrong account in a misconfigured-env scenario.

Not P0 today because the paper-account refusal (DU prefix) bounds the blast radius to paper accounts (no live-money corruption), and no order / position math is directly tied to the mismatched id at the broker boundary (the broker just routes by its own session). But every downstream surface that consumes the ledger reads stale identity, which is exactly the auditability-drift bucket P1 covers.

## Trading impact

- **Wrong-account fills**: an operator with IBKR_HOST/client_id misconfigured (e.g., `deployment_validation_2` inheriting env from `deployment_validation_1`'s account) deploys with `account_id="DU111"`, hashes that into `run_id`, then the engine submits orders to `DU222`. UI keeps showing `DU111`.
- **Multi-paper-account setups** (shared Gateway, multiple paper accounts on one user) route orders to whichever account IBKR binds to, not the operator's typed one.
- **Reconciliation drift**: account-ID-keyed reconciliation tools mismatch silently.
- **Provenance lies**: the `[START] account=…` log line, the bot control page "Account: …" field, and the per-trade audit all keep showing the operator-typed account.

## Reproduction

```bash
# Confirm the gate is paper-only, not identity:
sed -n '360,370p' PythonDataService/app/engine/live/live_engine.py

# Confirm self._account_id is set from ledger (operator-typed) and used for executions row:
grep -n "self._account_id" PythonDataService/app/engine/live/live_engine.py

# Confirm comparison pattern exists in emergency_flatten:
grep -n "connected_account" PythonDataService/app/engine/live/run.py | head
# (cmd_emergency_flatten has the comparison; cmd_start does not)
```

## Suggested resolution (NOT auto-applied)

After `client.connect()` succeeds and `self._connected_account` is populated in `_validate_paper_client`:

```python
if self._account_id and self._account_id.upper() != self._client.connected_account.upper():
    raise RuntimeError(
        f"Ledger account_id ({self._account_id}) does not match "
        f"broker-reported connected_account ({self._client.connected_account}). "
        "Check IBKR_HOST/client_id wiring."
    )
```

Map the error to `RunStatusSidecar.exit_reason=fatal_halt` with exit_code=2 so the bot control page's "Why it stopped" surface explains the mismatch. Pair with:

- A `session_started` event in `intent_events.jsonl` capturing BOTH `ledger.account_id` and `connected_account` for forensic traceability.
- Per-execution capture of `connected_account` alongside `ledger.account_id` so a tampered ledger cannot retro-rewrite history.
- A pre-flight test exercising `_validate_paper_client` with mismatched ids → expects halt.

Match the comparison pattern already established in `cmd_emergency_flatten`.

## Provenance of the finding

Lens: `run-ledger-identity-provenance` (workflow `wf_def78013-ce4`, structured-finding `ledger-account-not-verified-at-broker-connect`, verified 1/1 by adversarial pass). The reviewer confirmed all 9 cited evidence points, traced `_account_id` end-to-end, and noted the comparison pattern's presence in `cmd_emergency_flatten` as proof the team knows the pattern is appropriate.
