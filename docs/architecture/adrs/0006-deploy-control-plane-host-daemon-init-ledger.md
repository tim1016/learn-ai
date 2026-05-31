# ADR 0006 — Deploy (create-a-run) is a host-daemon control-plane operation, not a UI form; the content-addressed `run_id` is the idempotency key; the QC-backtest anchor is preserved (not relaxed)

**Status:** Accepted 2026-05-31 — QC-reference sourcing decided: **manual entry for v1** (see "Decision → QC anchor").
**Decision drivers:** A grilling session (2026-05-31, `docs/operator-deploy-ux-handoff.md`) scoped a "deploy/operate a strategy from the UI" effort and split it: the error-messaging pattern and the Start/Stop port ship now; **deploy (create-a-run) was deferred to this ADR** because it is not UI work. `init-ledger` — the only way to create a run today — runs a **git clean-tree check** and hashes **`git rev-parse HEAD`** plus a **`qc-cloud-backtest-id` that has no API** into the `run_id`. A "Deploy" button therefore means an HTTP request triggers git operations against a working tree and demands a QC reference with no source. That is a control-plane decision, not a form.
**Related:** ADR 0001 (JSON/Parquet substrate), ADR 0003 (host-venv operational topology), ADR 0004 (instance-addressed operator control plane), ADR 0005 (engine-authored readiness & broker ownership), `CONTEXT.md`, `docs/ibkr-paper-deployment-plan.md` § 16, `docs/operator-deploy-ux-handoff.md`.

## Context

Deploying a strategy is a **three-stage pipeline**. The operator console (ADR 0004) built stage 3; PR #410 retires the last UI affordance for stage 2 (ported into the console per the handoff); **stage 1 has never had a UI or an API**.

| Stage | Mechanism today |
|---|---|
| 1. Create the run | CLI `python -m app.engine.live.run init-ledger …` — writes `run_ledger.json`. **No API.** |
| 2. Launch the process | host daemon `POST /runs/{run_id}/start` (console Start/Stop port lands this in the UI). |
| 3. Observe/control | instance console (ADR 0004/0005). |

Stage 1 is the gap. Three properties of `init-ledger` make it fundamentally a control-plane operation rather than a CRUD endpoint:

1. **It requires a clean git working tree.** `cmd_init_ledger` calls `check_clean_tree(scope_paths, repo_root)` and **halts (exit 1)** if the tree is dirty (`run.py:109-112`). A run's provenance is meaningless if the code on disk doesn't match a committed state.
2. **The `run_id` is content-addressed over committed state.** `run_id = sha256(canonical_json({code_sha, strategy_spec_sha256, qc_audit_copy_sha256, qc_cloud_backtest_id, account_id, start_date_ms, live_config}))` (`run_ledger.py:98-123`). `code_sha` comes from `git rev-parse HEAD` (`run.py:114`). The identity of a run is *derived from the repo's committed state plus the inputs* — it is not a client-chosen primary key.
3. **The QC-backtest anchor is mandatory by design.** `--qc-cloud-backtest-id` and `--qc-audit-copy-path` are required (`run.py:1261-1275`). Every live run is anchored to a QuantConnect Cloud backtest for three-way reconciliation (numerical-rigor: "numerical claims require receipts"). There is **no QC-cloud API** in the repo — the operator supplies the id as a string and the audit copy as a file already placed under `references/qc-shadow/`.

**Where can `init-ledger` even run?** Verified: the `python-service` container (the data plane, where `live_instances.py` lives) mounts only `./PythonDataService/app`, fixtures, lean-data, cache, and `artifacts/` — **no repo root, no `.git`** (`compose.yaml:56-80`). The data plane *cannot* compute `git rev-parse HEAD` or run a clean-tree check; it has no working tree. The **host daemon** (`host_daemon.py`) runs as a host process in the repo venv (ADR 0003) — it is the only component that sits where the git working tree lives, and it already launches `run start` subprocesses on the host. This is decisive, not a preference.

## Decision

### 1. Deploy runs on the host daemon; the data plane forwards

`init-ledger` is exposed as a **host-daemon endpoint**, because only the host has the git working tree. The data-plane router forwards to it, mirroring the existing Start/Stop forwarding (`LiveRunsService.startHostRunner` → data plane → daemon `/runs/{id}/start`).

```
POST /api/live-instances              (data plane — forwards to daemon)
        → daemon POST /deploy         (host — runs init-ledger in-repo, optionally chains start)
```

The deploy logic is **extracted into a library function** so the CLI and the daemon endpoint share one path. `cmd_init_ledger` today interleaves arg-parsing, git checks, `build_ledger`, the run-dir-exists guard, `print`, and `sys.exit` return codes (`run.py:93-160`). Extract `deploy_run(params) -> DeployResult` that performs clean-tree → `code_sha` → `build_ledger` → write, raising **typed exceptions** (`DirtyTreeError`, `GitUnavailableError`, `SpecOrAuditMissingError`, `RunAlreadyExistsError`, `InvalidLiveConfigError`). The CLI maps them to its existing exit codes; the daemon maps them to HTTP status. No behavior change to the CLI; no `print`/`sys.exit` inside the shared function.

### 2. The content-addressed `run_id` is the idempotency key

Because `run_id` is a pure function of (committed code + inputs), deploy is **naturally idempotent**:

- Re-POSTing **identical inputs against the same HEAD** recomputes the same `run_id`. If its `run_dir` already exists, return **200** with the existing `run_id` and `created: false` — a safe no-op, not an error.
- The only "conflict" is a **non-deploy directory collision** (the run-dir exists but isn't a clean ledger, or `--force` semantics are requested). Surface **409** with the structured reason; `force` stays a deliberate, separately-authorized flag (rare per `run.py`), not a default.
- A different HEAD or any changed input yields a **different `run_id`** — by construction it cannot collide with an existing run. There is no "update a run" operation; runs are immutable once created (consistent with ADR 0004's evidence model and ADR 0001's substrate).

### 3. Deploy preconditions are first-class, surfaced via the agreed error pattern

Deploy has more preconditions than any other operation. They map onto the error-messaging pattern locked in the handoff (`(operation, HTTP status)` → category + remediation; inline; disable-with-visible-reason; shared connectivity strip):

| Precondition | Failure | Category | HTTP |
|---|---|---|---|
| Daemon reachable | daemon down | transient-infra | 503 |
| Git clean tree (scoped) | dirty tree | precondition-not-met | 409 |
| Git available / HEAD resolvable | git failure | transient-infra | 503 |
| Spec path + QC audit copy exist | missing file | validation | 400 |
| `live_config_json` is a valid object | bad JSON | validation | 400 |
| Run dir free (or `force`) | collision | domain-rejection | 409 |

The **dirty-tree blocker is the one most likely to confuse an operator** ("why won't Deploy work?"). Its remediation text must be explicit ("working tree dirty under `PythonDataService`, `references/qc-shadow` — commit or stash before deploying") and the dirty paths echoed from `CleanTreeResult.detail`.

### 4. QC anchor — preserved, not relaxed; **manual entry for v1**

The mandatory QC-backtest anchor is **not** weakened by this ADR. Relaxing it for paper/shadow was considered and **rejected**: the whole point of the anchor is three-way reconciliation, and the repo's standing rule is that numerical claims ship with receipts. A deploy that skips the anchor produces a run that can never be reconciled — exactly the artifact this platform exists to avoid.

**Decision: v1 sources the two QC inputs by manual entry.** The operator types `qc-cloud-backtest-id` and selects `qc-audit-copy-path` from a **host-side picker constrained to `references/qc-shadow/`** (the daemon lists that directory; the operator must have already committed the audit copy, which the clean-tree check enforces anyway). This changes *nothing* about the invariant — it lifts the existing CLI args into a form. The audit copy being a pre-placed, committed file is inherent to reconciliation-gating, not a UI shortcoming; the form states this in helper text.

Alternatives, recorded:

- **QC-cloud listing integration — deferred.** A QuantConnect Cloud API integration that lists the operator's backtests, lets them pick one, and auto-fetches the audit copy. The polished end-state, but a net-new authenticated external integration; tracked separately, not v1.
- **Relax for paper/shadow — rejected** as a default; if ever adopted it must be an explicit, separately-ADR'd carve-out for shadow-only runs with a loud "unreconciled" badge, never silent.

## Consequences

**Positive:**
- Deploy lands where it architecturally must (the host, with the git tree), instead of being forced into a data-plane container that cannot run git. No mounting of `.git` into a service container — a topology smell avoided.
- One shared `deploy_run` path means CLI and UI deploys are byte-identical in behavior and provenance; the content-addressed `run_id` gives idempotency for free, with no client-chosen keys to collide.
- The mandatory QC anchor — the reconciliation guarantee — survives the move to a UI. "Deploy" stays reconciliation-gated, matching the repo's philosophy.

**Negative:**
- A net-new host-daemon endpoint and the `cmd_init_ledger` → `deploy_run` refactor are required before any form exists. The daemon's surface grows from process-lifecycle to also "mint a run."
- The host-side `references/qc-shadow/` picker means the daemon gains a (tightly-scoped, read-only) directory-listing capability — validate the path stays within the scope root (the existing `_validate_path_segment`/`resolve`+`relative_to` barrier pattern applies; see CodeQL path-injection note).
- v1 deploy still presumes the operator has committed a QC audit copy and knows the backtest id — it is *not* one-click. That is inherent to reconciliation-gating, not a UI shortcoming, and should be stated in the form's helper text.

**Non-consequences:**
- Stage 2 (Start/Stop) is unaffected; it is handled by the console port in the handoff/PR #410.
- The on-disk substrate is unchanged (ADR 0001): the ledger still lands at `artifacts/live_runs/<run_id>/run_ledger.json`.
- This ADR does not unblock concurrent executing instances on one account (the deferred broker-executor separation, see ADR 0004 non-consequences).
- The QC-cloud listing integration is **deferred**, not decided here; it is a future enhancement tracked separately. v1 is manual entry.

## References

- `PythonDataService/app/engine/live/run.py:93-160` — `cmd_init_ledger` (to be refactored into shared `deploy_run`); `:114` `code_sha` from `git rev-parse HEAD`; `:109-112` clean-tree halt; `:1261-1275` required QC args.
- `PythonDataService/app/engine/live/run_ledger.py:98-123` — `compute_run_id` (the content-address).
- `PythonDataService/app/engine/live/host_daemon.py` — gains `POST /deploy`; already the host-side process authority.
- `PythonDataService/app/routers/live_instances.py` — gains `POST /api/live-instances` forwarding to the daemon.
- `compose.yaml:56-80` — `python-service` mounts (no repo root / `.git`): the constraint that forces deploy onto the host daemon.
- `docs/operator-deploy-ux-handoff.md` § "Resolution" — the grilling decisions this ADR extends.
