# PRD — Investigate and restore an operator-usable live-control topology

- **Status:** Draft — ready for investigation
- **Created:** 2026-07-17
- **Surfaces:** Account Desk, Journal cure, Broker Deploy, Bots/Bot Control, Strategy Validation, Angular development proxy, .NET Backend, Python data plane, host live-runner daemon, account Clerk
- **Primary outcome:** A trader can complete supported live-control work through the browser UI. If the UI cannot safely support a required operation, the product supplies one documented local command that performs or enables it.
- **Builds on:** ADR 0006 (deploy through the host daemon), ADR 0007 (daemon authentication), the Account Clerk RPC boundary, and `docs/audits/three-bot-concurrency-and-emergency-flatten-2026-07-17.md`.
- **Data authority:** Python remains the authority for broker/account state and mutations. Angular renders and submits backend-authored operations; it does not reproduce Clerk, deployment, or reconciliation logic.

---

## 1. Problem statement

The local application can boot into two materially different data-plane topologies without making the difference visible to the operator:

1. FastAPI runs on the host beside the host live-runner daemon and account Clerk.
2. FastAPI runs in the `python-service` container while the daemon and Clerk remain on the host.

Both topologies can answer some reads and forward some daemon HTTP operations. They are not currently equivalent for all live-control work.

The Account Desk journal-cure endpoint first asks the host daemon to ensure the Clerk is ready and then opens a Unix socket directly from the FastAPI process. The socket path depends on both the process-local temporary directory and the process-visible artifact-root path. A container process sees `/app/artifacts` and a container-local `/tmp`; the host Clerk sees the host artifact path and host temporary directory. The resulting socket locations differ, so a containerized data plane can report `SOCKET_MISSING` even while the host daemon and Clerk are healthy.

There is a second, partially disputed symptom around deploy sources. The container does not mount the repository's `references/` tree, but QC audit-copy enumeration is designed to be forwarded to the host daemon and the currently validated deployment strategy has a bundled audit-copy fallback. An empty strategy catalog or QC audit-copy list therefore cannot be attributed to the missing mount without tracing the exact request path, authentication result, process version, and daemon response.

Operationally, moving FastAPI to the host restores Clerk socket affinity. However, the Angular and .NET containers may continue targeting the stopped `python-service` service name. In that state, direct requests to host port 8000 succeed while browser requests through port 4200 fail. The operator sees a broken UI even though the underlying host data plane is healthy.

The product currently lacks a single supported answer to:

> What must I run, and what must be green, before the browser can safely deploy, start, stop, reconcile, and apply an Account Desk cure?

## 2. Product goal

Identify and prove the smallest repeatable local workflow that lets an operator use the existing UI for live-control work.

The investigation must finish with one of these outcomes, in priority order:

1. **UI-native:** the normal local UI starts in a topology where every advertised live-control action works.
2. **UI enabled by one local command:** one copyable command starts or retargets the necessary local processes, after which the normal UI works.
3. **Command fallback for an isolated operation:** when an operation cannot safely be offered through the UI, one copyable host command performs that operation through the existing Python authority and returns a typed receipt.

The investigation is not successful if it ends only with a diagram, a proposed redesign, or a list of manually coordinated processes.

## 3. Operator stories

1. As an operator, I open the local UI and can tell whether live control is available before I submit a mutation.
2. As an operator, I can open Strategy Validation and see the registered strategies rather than an unexplained empty list.
3. As an operator, I can open Deploy and see the committed QC audit copies available to the host daemon.
4. As an operator, I can deploy and start an approved paper canary through the UI without choosing a path that exists only inside the wrong filesystem namespace.
5. As an operator, I can preview and confirm an eligible journal cure through Account Desk, and the receipt comes from the active account Clerk.
6. As an operator, when UI live control is unavailable, I receive one exact local command plus an explanation of what it will start, stop, or mutate.
7. As an operator, I can run one verification command that tells me whether the UI, data plane, daemon, Clerk, artifacts, and deploy sources agree before I trade.
8. As an auditor, I can reconstruct which process owned port 8000 and which topology was active when an operation succeeded or failed.

## 4. Investigation principles

- **Operator outcome over topology preference.** Do not begin by selecting host-only or container-only as the desired architecture. Measure which supported workflow satisfies the UI and mutation contracts with the fewest operator steps.
- **Trace real request paths.** Test browser-originated requests through the Angular proxy, not only direct `curl` calls to port 8000.
- **Separate facts from hypotheses.** Record the exact HTTP status, typed reason code, upstream target, and process owner. Do not infer “missing source” from an empty UI control.
- **No second mutation implementation.** A command fallback must call the existing FastAPI/Clerk/daemon authority; it must not edit journals or ledgers directly.
- **Fail safely.** No investigation step may place a live-account order. Paper mutations require explicit operator approval and the existing paper fences.
- **Redact configuration evidence.** Rendered Compose configuration and process environments must never capture API keys, database passwords, daemon tokens, or control secrets in the investigation report.

## 5. Known evidence to verify

The following are inputs to the investigation, not conclusions that may be copied forward without reproduction:

| Observation | Current evidence | Required verification |
|---|---|---|
| Clerk socket identity differs across host/container | Socket helper hashes the account artifact path and uses `tempfile.gettempdir()` | Compute both locations for the same account and record existence without exposing the account when the evidence is published externally |
| Container cure returns `SOCKET_MISSING` | `AccountClerkRpcClient` checks its derived path before connecting | Exercise the HTTP cure path or a non-mutating generation handshake from each topology |
| Daemon HTTP remains reachable from the container | Start/stop and Clerk readiness use the host-daemon client | Record daemon health and one authenticated read from the data-plane process |
| `references/` is absent in `python-service` | Compose volumes omit the repo `references/` directory | Record container filesystem visibility and distinguish it from host-daemon visibility |
| QC audit-copy listing is daemon-owned | FastAPI route forwards to daemon `/qc-audit-copies` | Capture data-plane-to-daemon status, response count, and any authentication failure |
| Strategy validation has a bundled fallback | `app/data/qc-shadow` contains the deployment-validation audit copy | Verify hashes and catalog behavior inside the actual container image/process |
| Host FastAPI restores direct Clerk RPC | Host and Clerk share artifact spelling and temporary namespace | Complete a generation handshake and, only with an eligible stale claim, a UI cure |
| Browser proxy may still target `python-service` | Frontend environment defaults to the Compose service | Record the effective target and request result after each restart path |

## 6. Questions the investigation must answer

### 6.1 Startup and restart ownership

1. Which command, service, or restart policy starts FastAPI after:
   - `podman compose up`;
   - `podman compose restart`;
   - a machine reboot;
   - a host-daemon restart;
   - the current documented paper-trading startup procedure?
2. Can both host and container FastAPI processes contend for host port 8000?
3. Does `restart: always` silently restore the container topology after an operator previously used the host topology?
4. Is the active topology visible through a health or capability response, or only inferable from process inspection?

### 6.2 UI request routing

1. What effective `DATA_PLANE_PROXY_TARGET` does the running frontend use?
2. What effective Python base URL does the .NET backend use?
3. Can the frontend container reach host FastAPI, and is that target accepted by the proxy's trusted-host guard?
4. Which operator pages call FastAPI directly through `/api`, and which depend on GraphQL or backend-to-Python calls?
5. When a browser request fails, does the UI distinguish a proxy failure, daemon failure, missing source, and Clerk socket failure?

### 6.3 Clerk mutation boundary

1. Which existing UI actions open `AccountClerkRpcClient` from FastAPI rather than forwarding the complete mutation to the host daemon?
2. For each such action, what topology capability must be true before the UI advertises it?
3. Can a read-only Clerk generation handshake serve as the preflight for those controls?
4. Does the UI retain enough typed failure detail to tell the operator to switch topology or run the fallback command?

### 6.4 Strategy and audit-copy sources

1. Why did the reported catalog and QC audit-copy requests return empty?
2. Were the responses truly HTTP 200 empty payloads, fail-closed projections after an unreachable daemon, authorization failures, proxy failures, or results from an older process version?
3. Which files must be visible to FastAPI itself, and which only need to be visible to the daemon?
4. Does deploy preflight make the same deployability decision in host and container topologies for `deployment_validation`?
5. Can the current UI deploy by sending repo-relative paths that only the host daemon resolves?

## 7. Required topology matrix

Run the same evidence harness against each applicable row. A row may be rejected early if it cannot meet the paper-safety or single-port-owner prerequisites.

| ID | FastAPI | `python-service` | Frontend | Backend → Python | Daemon + Clerk | Expected purpose |
|---|---|---|---|---|---|---|
| H/H | Host port 8000 | Stopped | Host | Host process explicitly targeting host FastAPI | Host | Fully co-located baseline; proves the entire browser and Backend assembly can start without Compose `python-service` |
| H/C | Host port 8000 | Stopped | Container, explicitly targeting host | Container explicitly targeting host FastAPI and able to start without `python-service` | Host | Preferred container-UI workflow if proxy and Backend policy permit it |
| C/C | Container | Running | Container, targeting `python-service` | Container targeting `http://python-service:8000` | Host | Current Compose-default behavior and failure reproduction |
| C/H | Container | Running | Host, targeting port 8000 | Container targeting `http://python-service:8000` | Host | Control case proving that moving only the frontend cannot repair Clerk RPC |

For every tested row, record:

- owner of host port 8000;
- whether `python-service` is running and healthy;
- FastAPI working directory and artifact root;
- temporary directory and derived Clerk socket identity;
- daemon reachability and authenticated health;
- Clerk generation handshake result;
- strategy-validation count and deployable count;
- QC audit-copy count;
- direct data-plane result;
- browser-proxy result;
- Backend process owner, effective Python base URL, and startup dependency state;
- backend-to-Python result for one representative backend-dependent read;
- whether Deploy, Start/Stop, Reconcile, cure preview, and cure confirmation are available;
- exact typed failure for every unavailable operation.

## 8. Investigation slices

### Slice 1 — Reproduce and timestamp the topology transition

Build a read-only evidence script or documented command sequence that identifies the process owning port 8000, Compose service state, daemon/Clerk processes, effective Angular and Backend Python targets, and relevant health endpoints. Run it before and after each supported restart procedure.

**Exit criterion:** the investigation can state exactly which restart changes a working host topology into the container topology.

### Slice 2 — Trace UI-to-authority requests

Trace these browser operations end to end:

1. Strategy Validation catalog read.
2. QC audit-copy picker read.
3. Deploy preflight.
4. Bot Start/Stop readiness.
5. Account Desk journal-cure preview.
6. Account Desk journal-cure confirmation up to the Clerk boundary.

For each, name every hop and filesystem/process dependency. Capture typed outcomes at the browser proxy, Backend, FastAPI, daemon, and Clerk boundaries.

**Exit criterion:** an empty response or disabled control has one proven cause, not a topology-based guess.

### Slice 3 — Prove the smallest UI workflow

Test the lowest-coordination candidate first:

1. Keep the daemon and Clerk on the host.
2. Run FastAPI on the host with the canonical host artifact root.
3. Start or explicitly retarget the Backend so its Python base URL reaches that
   host FastAPI without depending on the stopped `python-service` container.
4. Make the normal Angular UI target that complete host-FastAPI/Backend assembly.
5. Verify the full UI acceptance flow in §9.

This slice may use a temporary command before any repository change. Record the exact command, foreground/background behavior, logs, rollback, and what happens on the next Compose restart.

**Exit criterion:** the UI workflow is reproducible from a clean local state without hand-editing sockets, journals, ledgers, or container files.

### Slice 4 — Prove the command fallback

For any action that cannot pass Slice 3, provide one local command that:

- runs on the host;
- calls an existing authenticated Python authority;
- performs a read/preview before a mutation when the UI does;
- preserves idempotency and typed receipts;
- prints the receipt or a typed failure;
- does not require the operator to calculate a socket path;
- does not read or write journal JSONL directly.

**Exit criterion:** a non-UI operation is executable with one copy/paste block and has a documented rollback or retry rule.

### Slice 5 — Recommend the minimum product change

Only after the matrix is complete, recommend the smallest change required to make the winning workflow repeatable. Candidate outputs may include:

- a checked-in launcher command for host FastAPI + host Angular;
- a Compose override/profile that points the UI at host FastAPI;
- a proxy trusted-host adjustment;
- a topology capability preflight that hides or blocks unsupported controls;
- a copyable Account Desk fallback command.

The recommendation must be justified by measured operator steps and failure recovery, not architectural neatness.

**Exit criterion:** the user can choose a concrete change with its exact operational tradeoffs.

## 9. UI acceptance flow

The winning topology must pass this flow from `http://localhost:4200` using real browser-originated requests:

1. The application loads with no `/api` proxy error.
2. Strategy Validation returns the registered catalog and identifies `deployment_validation` according to its current evidence.
3. Deploy returns the daemon-authored QC audit-copy listing.
4. Daemon health and account Clerk status are visible and current.
5. Account Desk loads the selected paper account.
6. Journal cure preview reaches the host artifacts and returns a typed actionable/non-actionable verdict.
7. If a safely reproducible retired-namespace stale claim exists, the operator confirms exactly one cure and receives a Clerk journal sequence receipt. Otherwise, the full mutation is proven with a test fixture and the live UI stops at preview.
8. Deploy preflight for the one-share paper canary returns its real verdict.
9. A deploy/start mutation is executed only with explicit operator approval and existing paper/read-only fences.
10. Refreshing the UI shows the same durable outcome reported by the receipt.

Direct port-8000 success does not substitute for this browser acceptance flow.

## 10. Command-fallback acceptance criteria

If the investigation concludes that a required action cannot be safely exposed through the current UI, the fallback must include:

1. One copyable command block run from the repository root, or a block that
   derives it with `git rev-parse --show-toplevel`.
2. A topology-enabling command preflights the required port-8000 owner and host
   Clerk. An isolated operation fallback preflights only the authority it uses;
   it must not reject solely because an unrelated UI process owns port 8000.
3. No secrets embedded in the command or printed to the terminal.
4. Explicit paper-account and confirmation requirements for broker mutations.
5. Idempotency behavior documented for retry after a timeout or ambiguous response.
6. A typed success receipt and a non-zero exit code on failure.
7. A statement of whether the command leaves a foreground process running and how to stop it.

## 11. Evidence artifact

The investigation produces one dated report under `docs/audits/` containing:

- the completed topology matrix;
- reproduction commands with secrets redacted;
- relevant HTTP status and typed reason codes;
- process/port ownership before and after restart;
- the proven cause of the earlier empty catalog and QC listing;
- screenshots or concise browser evidence for the winning UI flow;
- the chosen UI startup command or operation fallback;
- known failure and rollback behavior;
- follow-up product changes, ranked by operator value and effort.

Do not store complete process environments, rendered secret-bearing Compose configuration, broker credentials, or account tokens in the report.

## 12. Definition of done

- The host/container Clerk socket mismatch is reproduced and explained with code and runtime evidence.
- The earlier empty catalog and QC audit-copy symptoms have a proven request-level cause.
- All four applicable topology rows have a verdict, or an explicit safety reason they were not run.
- One topology passes the browser UI flow, or the report proves why that is not currently possible.
- The operator receives either:
  - a normal UI that works after the standard startup; or
  - one local command that enables the UI; or
  - one local command for each isolated action that cannot be made available through the UI.
- Restart behavior is tested, including whether Compose silently restores the broken topology.
- The recommended next change is the smallest measured operator remedy; a broader architecture redesign is optional and separately scoped.
- No live-account order is placed, no journal is edited directly, and no secret is captured in evidence.

## 13. Non-goals

- Selecting a permanent cross-platform service architecture before investigation.
- Replacing Unix sockets, the Clerk protocol, the daemon protocol, or the deployment authority as part of the investigation.
- Mounting the entire repository into a container without first proving it is required.
- Moving broker or reconciliation logic into Angular or .NET.
- Auto-curing stale exposure.
- Re-litigating the numerical correctness of the validated strategy.
- Testing real-money trading.
- Making unrelated Compose, frontend, or backend cleanup changes.

## 14. Open decisions after investigation

1. Whether the supported local live-control mode should run the frontend on the host or retain it in a container targeting host FastAPI.
2. Whether one launcher command should own host FastAPI and the host frontend together.
3. Whether Compose should expose a distinct live-control profile that omits `python-service` and changes dependent targets.
4. Whether unsupported Clerk-direct controls should be hidden at startup or remain visible with a copyable host-command remedy.
5. Whether non-bundled validation evidence genuinely requires a `references/` mount or should remain host-daemon-owned.

## 15. Code and document references

- `PythonDataService/app/engine/live/account_clerk.py` — socket-path derivation.
- `PythonDataService/app/engine/live/account_clerk_rpc.py` — client availability and generation handshake.
- `PythonDataService/app/routers/account_reconciliation.py` — journal-cure HTTP boundary.
- `PythonDataService/app/routers/live_instances.py` — deploy forwarding and QC audit-copy proxy.
- `PythonDataService/app/engine/live/host_daemon.py` — host deploy and audit-copy authority.
- `PythonDataService/app/services/strategy_validation_manifest.py` — validation evidence and bundled fallback resolution.
- `Frontend/proxy.conf.js` — data-plane target validation and control-secret attachment.
- `compose.yaml` and `compose.override.yaml` — process startup, dependencies, volumes, and proxy targets.
- `docs/audits/three-bot-concurrency-and-emergency-flatten-2026-07-17.md` — incident evidence and initial topology hypothesis.
