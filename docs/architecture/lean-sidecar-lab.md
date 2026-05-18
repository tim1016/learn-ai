# LEAN Sidecar Lab

**Status:** Phase 5b — reconciliation-grade template + Phase 5a self-reconciler shipped; Phase 1c sandbox + Phase 4a-e UI complete
**Last reviewed:** 2026-05-17
**Pairs with:** `docs/architecture/engine-authority-map.md`, `docs/references/lean-engine.md`, `.claude/rules/numerical-rigor.md`

This document is the authority for the **LEAN Lab** feature — a UI surface in learn-ai where the user pastes or edits a real `QCAlgorithm` and runs it through an isolated official LEAN runner sidecar. It exists because the alternative ("just shell out to LEAN from FastAPI") quietly violates several invariants this repo depends on.

Phase 0 landed this decision record plus the matching row in
`docs/architecture/engine-authority-map.md`. Phase 1 (this PR) lands
the runnable surface: launcher service, podman-shape runner with the
proven hardening flags, manifest writer, workspace contract, LEAN
data-folder staging, image-bundled metadata extraction, trusted
`MyAlgorithm` Python sample, the data-folder round-trip fidelity
fixture, the security-flag viability matrix, and three end-to-end
sidecar runs (baseline, with `--cap-drop=ALL`, and the xfailed
`--read-only` documented in the security section).

**Pinned LEAN image digest (Phase 1b):**
`sha256:97884667be20077925996ac22b5e3e16e3a47e7363e01795151459d16786247c`

---

## TL;DR

- LEAN Lab is a **sidecar reference runner**, not a new canonical engine.
- The Python Engine Lab (`app/engine/`) remains the canonical event-driven engine per `engine-authority-map.md`. That row does not change.
- User-supplied `QCAlgorithm` code executes **only** inside a disposable, network-isolated LEAN container with the smallest validated capability surface — never as a child process of `polygon-data-service`, the .NET backend, or Angular.
- The control-plane (whatever invokes `podman run`) is a separate small launcher service with Podman API access. The data-plane container (`polygon-data-service`) never receives the podman socket.
- The user-facing path is the `/lean-lab` page in the Frontend. There is no user-facing CLI.

---

## Authority boundary — what LEAN Lab is and is not

| Question | Answer |
|---|---|
| Is this the canonical backtest engine? | **No.** `app/engine/` remains canonical. See `engine-authority-map.md` row "Interactive backtest (stocks, indicator strategies)". |
| Does Engine Lab math get re-derived from LEAN runs? | **No.** Engine Lab math is canonical and pinned against the vendored extract at `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/`. |
| What is LEAN Lab for? | (a) Running user-authored `QCAlgorithm` code for **compatibility** with QC's ecosystem. (b) Producing reference traces for **reconciliation** against Engine Lab on shared strategies. (c) Capturing **audit evidence** of "what real LEAN does on this input". |
| Can LEAN Lab become canonical later? | Only via a deliberate change to `engine-authority-map.md` and `math-sources-of-truth.md` in the same PR. Not by drift. |
| Where do LEAN Lab outputs go in the math registry? | They do not. Outputs of arbitrary user-authored `QCAlgorithm` code are not registered math. The runner itself (the LEAN image at a pinned digest) is the reference; per-run outputs are evidence, not authority. |

---

## Non-negotiables (operationalized)

These are the invariants. Every Phase 1–6 PR re-asserts them in its description.

| # | Invariant | How enforced |
|---|---|---|
| 1 | User code never runs in `polygon-data-service`, `my-backend`, or `my-frontend` | Test: `runner.py` integration test asserts the container invocation; no in-process evaluation of user source in the data plane. |
| 2 | User code runs only inside a disposable container | Same as #1. The runner's only execution path is `podman run --rm ...`. |
| 3 | Container has no network | `--network=none` is non-conditional in the runner. |
| 4 | Repo root is never mounted | The runner accepts a single absolute workspace path and rejects any path not under the configured artifacts root. |
| 5 | No secrets in the run | The runner constructs the container environment from a fixed allow-list; `.env`, host home, `$HOME/.config`, and the podman socket are never mounted. |
| 6 | Resource limits are mandatory | `--cpus`, `--memory`, `--pids-limit`, disk cap, and wall-clock timeout are required parameters of the runner; tests assert the runner refuses to launch without them. |
| 7 | UI is the primary surface | `/lean-lab` page is the product path. CLI scripts exist only as developer-internal tools (e.g., the Phase 1 spike), are not packaged, and are not documented in the user-facing README. |
| 8 | All timestamps crossing the API boundary are `int64 ms UTC` | Per `.claude/rules/numerical-rigor.md` → "Timestamp rigor". Applies to request, response, and persisted manifest. |
| 9 | LEAN data encoding is part of the contract | Phase 1 adds a fixture that writes LEAN-format data, runs LEAN, and proves the algorithm sees the intended prices/timestamps. No sidecar run may write ad hoc CSV dollars or epoch timestamps. |
| 10 | Corporate-action mode is explicit | Runs declare `data_adjustment_policy` in the manifest. Reconciliation-grade runs use raw bars plus LEAN factor/map files; adjusted bars without a matching policy are rejected for reconciliation. |
| 11 | Reconciliation-grade runs pin brokerage/fill/fee semantics | General LEAN Lab runs may execute the user's algorithm as written. Any run compared against Engine Lab must pin brokerage and fill assumptions in the algorithm/template and manifest. |
| 12 | LEAN output parsing is a timestamp ingestion boundary | LEAN's result-series timestamps are unix seconds (often as floats); the normalized parser converts them to `int64 ms UTC` at the ingestion boundary in `app/lean_sidecar/normalized_parser.py::_unix_seconds_to_ms_utc`, and every downstream API response and persisted artifact carries `int64 ms UTC` only. |
| 13 | Reconciliation-grade subscriptions disable fill-forward | LEAN's default minute subscription can synthesize forward-filled bars. Engine Lab's rigor rules forbid forward-fill alignment, so reconciled algorithms/templates must request `fillForward=false` and the manifest records it. |
| 14 | Reconciliation-grade subscriptions pin normalization mode | LEAN's staged data policy and runtime `DataNormalizationMode` are independent. Reconciled algorithms/templates must set the normalization mode to match Engine Lab and the manifest records it. |
| 15 | Reconciliation fixtures require determinism proof | Phase 1 runs the trusted sample twice with the same image/data/config and asserts equivalent artifacts before any golden fixture or reconciliation claim is accepted. |
| 16 | Algorithm date range must overlap staged data and consume bars | The request range, LEAN effective algorithm range, and staged data window are recorded separately. Runs that consume zero bars for a requested subscription fail fast; reconciliation-grade runs require exact window alignment. |

---

## Container execution boundary

The non-negotiable shape of every LEAN sidecar invocation:

```bash
podman run --rm \
  --network=none \
  --cpus=2 \
  --memory=2g \
  --pids-limit=512 \
  --security-opt=no-new-privileges \
  --cap-drop=ALL \
  -v <absolute_host_workspace_path>:/lean-run:rw \
  <pinned-lean-runner-image> \
  <runner-command>
```

Additional flags applied **if compatible** with the chosen LEAN image (validated in Phase 1):

- `--cap-drop=ALL` — currently listed in the required shape above; Phase 1 verifies the image tolerates it. If not, the runner must use the smallest documented capability allow-list and this ADR must be updated in the same PR.
- `--read-only` — root filesystem read-only
- `--tmpfs /tmp:rw,noexec,nosuid,size=256m`
- `--user <non-root-uid>` — drop root inside the container
- `--storage-opt size=<cap>` — defense-in-depth for writes to the container overlay, when supported

Phase 1's runner spike must record which of these flags the LEAN image actually tolerates. If `--read-only`, `--cap-drop=ALL`, or non-root breaks LEAN, the doc gets updated with the smallest accepted relaxation. The workspace-only mount, no-network rule, no-secrets rule, and hard CPU/memory/time/disk ceilings remain mandatory.

### Wall-clock timeout

The launcher enforces an outer timeout (kill the container) **in addition to** any LEAN-internal timeout. Defaults — verify on first run, then pin:

- Per-run timeout: **120s** (configurable in the request, capped at a server-side max).
- Log capture cap: **8 MB** truncated tail returned in the API; persisted logs count against the per-run disk cap.
- Per-run source-size cap: **256 KB** for `algorithm_source` (rejects oversized payloads at request validation).
- Per-run workspace-size cap: enforced by the launcher by monitoring the bind-mounted workspace directory and killing the container when the cap is exceeded. `podman --storage-opt size=...` does **not** cap bind-mounted output and is only defense-in-depth for non-mounted writes. The cap is recorded in `manifest.json`.

---

## Workspace contract

Each run owns a fresh directory under the artifacts root. The data-plane container (`polygon-data-service`) writes into it; the launcher service mounts **only this directory** into the LEAN container.

```text
PythonDataService/artifacts/lean-sidecar/<run_id>/
  workspace/                       # the only path mounted into the LEAN container
    project/
      main.py    | Main.cs         # user-submitted QCAlgorithm source
      config.json                  # LEAN config we author
    data/
      equity/usa/minute/<symbol>/  # LEAN-format zips from polygon_export.py
      equity/usa/factor_files/     # required for reconciliation-grade adjusted equity runs
      equity/usa/map_files/        # required for reconciliation-grade mapped symbols
      market-hours/market-hours-database.json
      symbol-properties/symbol-properties-database.csv
    output/                        # LEAN writes here; launcher never overwrites these files
      logs.txt
      <statistics>.json
      <orders>.json
    launcher/
      launcher.log                 # container stdout/stderr + launcher diagnostics
  normalized/                      # learn-ai DTOs parsed from output/
    result.json
  manifest.json                    # run_id, image digest, input hashes, limits, timestamps (int64 ms UTC)
```

The repo root is never mounted into the LEAN container. The `data/` subtree is **pre-populated** by `polygon-data-service` before the runner is invoked; the LEAN container does no network I/O.

### LEAN data-folder fidelity

The data folder is not "some CSVs under `data/`"; it is a numerical contract.
For US equity minute data, the writer must match `PythonDataService/app/engine/data/lean_format.py`:

- Zip path: `equity/usa/minute/<symbol_lower>/<YYYYMMDD>_trade.zip`.
- Zip entry: `<YYYYMMDD>_<symbol_lower>_minute_trade.csv`.
- No header; columns are `ms_since_midnight_et,open,high,low,close,volume`.
- The time column is milliseconds since midnight in the exchange timezone (`America/New_York` for US equities), **not** epoch milliseconds.
- Prices are integer deci-cents: `price * 10000`. Raw dollar prices are forbidden because LEAN will divide them by 10000 and still complete the run with garbage prices.
- Phase 1 adds a round-trip fixture that writes a tiny deterministic price series, runs a QCAlgorithm that records the prices it receives, and asserts the observed values match the intended dollar prices with the LEAN quantization floor below.

Daily, quote, option, future, and extended-hours data layouts are out of scope
until a Phase 1+ PR pins their exact LEAN layout with the same kind of fixture.
A UI request for an unsupported resolution/security type, quote-dependent
algorithm, or extended-hours run must fail fast, not silently stage a
best-effort folder.

### Corporate actions and metadata policy

Reconciliation-grade equity runs use exactly one policy:

1. Fetch/stage **raw, unadjusted bars** (`adjusted=false` at the Polygon boundary).
2. Stage matching LEAN-format factor files and map files for every requested symbol.
3. Stage the market-hours and symbol-properties databases used by the pinned LEAN image, then hash them into the manifest.

Pre-adjusted bars without matching neutral factor/map files are allowed only for
non-reconciliation exploratory runs and must be labeled
`data_adjustment_policy=pre_adjusted_non_reconciliation` in the manifest and UI.
They cannot be compared against Engine Lab as "exact".

If factor/map files are unavailable, a reconciliation-grade run is rejected
with an explicit error. This avoids the two silent failure modes:

- adjusted bars plus LEAN factor files => double adjustment
- adjusted bars without factor files => LEAN raw-mode semantics over adjusted input

The metadata databases are also part of the reproducibility surface. The
sidecar must not silently rely on image-baked defaults for runs that claim
reconciliation parity; the exact staged files and hashes go into `manifest.json`.

---

## Launcher topology — why a separate service

The data-plane container `polygon-data-service` runs inside podman (see `compose.yaml:43`). To launch a *sibling* LEAN container from inside it, there are exactly two options:

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **A.** Mount the podman socket into `polygon-data-service` | One less moving part | A successful exploit of any FastAPI handler that touches the socket = full host control. Directly violates non-negotiable #5. | **Rejected.** |
| **B.** Separate **launcher service** that owns the socket/API access | Privileged surface is one tiny service. `polygon-data-service` keeps its current attack surface. Launcher can refuse any path not under the artifacts root. | One new service to write, deploy, and authenticate. Windows/Podman placement must be proven in Phase 1. | **Chosen.** |

### Launcher shape (Phase 1 commits to specifics)

- Runs beside the data-plane with access to the Podman API — **not** inside `polygon-data-service`.
- Accepts a request over a **unix domain socket** bind-mounted into `polygon-data-service` (Linux/macOS) **or** localhost + shared-secret token (Windows dev hosts). The Windows token path is documented as the fallback and not used in any production-shaped deployment.
- Request payload is minimal — `{ run_id, image, limits }`. The launcher resolves `run_id` to a host-absolute workspace path itself using its configured artifacts root; the data plane never sends paths.
- Validates the resolved path is under the configured root, the workspace directory exists, and the requested image matches an allow-list of pinned digests.
- Invokes `podman run` with the flags in *Container execution boundary* above.
- Returns `{ exit_code, duration_ms, log_tail }` and persists container stdout/stderr plus launcher diagnostics to `workspace/launcher/launcher.log`. LEAN owns `workspace/output/logs.txt`; the launcher must never overwrite LEAN artifacts.

Phase 1 starts with the simplest launcher placement that can actually invoke Podman in the current dev environment. A future hardening pass can move it into its own minimal container with **only** the Podman socket/API endpoint and the artifacts root mounted.

### Windows + Podman topology

The primary dev environment is Windows with Podman. That means "host-side"
cannot assume the Linux layout where `/var/run/podman/podman.sock` and the
workspace path live in the same namespace. The Podman API may live inside the
WSL2/podman-machine VM, while the repo lives under `C:\Users\...` and is
presented to containers through a VM mount.

Phase 1 therefore has a topology gate before any arbitrary user source is
accepted:

- Decide where the launcher runs on Windows: native Windows process,
  WSL2/podman-machine process, or a small launcher container with only the
  Podman socket and artifacts root mounted.
- Prove host path resolution from `run_id` to the exact path mounted into the
  LEAN container. The data-plane still never sends arbitrary paths.
- Prove UID/GID behavior for the mounted workspace before enabling
  `--user <non-root-uid>`.
- Record the Windows topology in this ADR and in the launcher test fixture.

Until that gate passes, Phase 1 may run only the trusted sample algorithm.

---

## Runner choice — image, version, and the CLI question

The plan explicitly asks whether `lean backtest` (the official CLI from `lean-cli`) is acceptable. Decision:

- **Use the Docker image directly,** not the CLI, for the first implementation.
- Pinned image: `quantconnect/lean` at a specific digest resolved in Phase 1 and recorded here. The plan calls for `:latest` initially — that is acceptable for the Phase 1 spike, but the merged Phase 1 PR replaces `:latest` with a `sha256:...` digest in code and in this doc.
- The official `lean-cli` is a convenience wrapper that ultimately invokes the same image, and it adds account/auth steps and login state we don't want in a CI/server flow. Calling the image directly keeps the dependency surface to "podman + a pinned image digest".

### LEAN compatibility posture

Avoiding the CLI does **not** mean inventing a learn-ai-specific algorithm
runtime. The sidecar mirrors LEAN's local project conventions as closely as the
container boundary allows:

- User algorithms live in `main.py` or `Main.cs`.
- The submitted class is a real `QCAlgorithm` subclass.
- Run parameters flow through LEAN's parameter mechanism, not custom globals.
- Market data is staged in LEAN's local data-folder shape before launch.
- Raw LEAN result artifacts are preserved before learn-ai normalizes anything.

The UI replaces the user's terminal workflow; it does not replace LEAN's
algorithm contract.

Python algorithms are the Phase 1 target. C# algorithms remain planned but
gated: Phase 1 must prove `Main.cs` compilation works under `--network=none`
without NuGet restore, or the UI keeps C# disabled with a documented reason.

All bundled trusted samples and templates use the `MyAlgorithm` entry class
until the UI supports an explicit `algorithm_type_name` field.

### Config the launcher writes

The LEAN runner is driven by a `config.json` the data-plane authors and writes into `workspace/project/config.json`. The minimum fields LEAN needs to backtest from local data:

- `algorithm-language` (Python or C#)
- `algorithm-type-name` (entry class — defaults to `MyAlgorithm`)
- `algorithm-location` (path inside the container — `/lean-run/project/main.py` or compiled assembly)
- `data-folder` — `/lean-run/data`
- `results-destination-folder` — `/lean-run/output`
- The LEAN parameters dict, populated from the request's `parameters` map

Phase 1 confirms the exact key names against the pinned image and records the canonical config template in this doc.

Do not auto-detect the entry class with a regex over arbitrary source. The
first UI version uses a required convention: the entry class is `MyAlgorithm`
unless the request explicitly sets `algorithm_type_name`. Preflight may reject
obvious mismatches, but LEAN remains the arbiter of which algorithm type is
loaded.

### Brokerage, fill, and fee policy

There are two run classes:

- **Compatibility run** — executes the user's `QCAlgorithm` as written. If the
  user does not call `SetBrokerageModel`, the result is whatever the pinned
  LEAN image's default brokerage/fill/fee model produces. The manifest records
  `brokerage_policy=algorithm_default`.
- **Reconciliation-grade run** — eligible for Engine Lab comparison. The
  algorithm/template must explicitly pin brokerage and account type (for the
  current Engine Lab parity target: Interactive Brokers brokerage, matching
  account type, and the documented fill/fee assumptions). The manifest records
  the brokerage/fill/fee policy, and Phase 5 reconciliation uses those fields
  to decide whether `COMMISSION_DRIFT` is gating or diagnostic.

No run may be labeled "exact" against Engine Lab unless the brokerage/fill/fee
policy is explicit.

Reconciliation-grade runs also pin starting capital and account currency. The
trusted sample template must call `SetCash(<amount>)` (and any required account
currency setting supported by the pinned LEAN version) to match the Engine Lab
request. Otherwise `SetHoldings(...)` target sizing can diverge even when the
signals and fill prices match.

### Fill-forward policy

LEAN subscriptions can forward-fill missing minute bars by default. Engine Lab's
numerical-rigor rule forbids forward-fill/interpolation alignment, so
reconciliation-grade algorithms/templates must subscribe with `fillForward=false`
and the manifest records `fill_forward=false`.

Compatibility runs may use the user's requested/default fill-forward behavior,
but those outputs are not eligible for exact Engine Lab reconciliation unless
Engine Lab is explicitly configured to observe the same synthetic bars and the
exception is documented in the reconciliation report.

### Data normalization mode policy

The bar staging policy and LEAN's runtime normalization mode are separate
controls. Reconciliation-grade equity algorithms/templates must set
`DataNormalizationMode.Raw` unless the Engine Lab run being compared explicitly
uses adjusted prices. The manifest records both:

- `data_adjustment_policy` — how files were staged (`raw_with_factor_map_files`,
  `pre_adjusted_non_reconciliation`, etc.)
- `data_normalization_mode` — how LEAN presented prices to the algorithm
  (`Raw`, `Adjusted`, etc.)

This prevents the silent divergence where raw bars and factor/map files are
staged correctly, but `AddEquity(...)` defaults to adjusted runtime prices and
the algorithm sees a different price series than Engine Lab.

### Date-window and bar-consumption policy

LEAN can complete a backtest even when the algorithm's effective
`SetStartDate`/`SetEndDate` range does not overlap the staged data files. That
is a silent-green failure, not a valid backtest.

The sidecar records three windows independently:

- `requested_window_ms` — what the UI/API asked to run.
- `staged_data_window_ms` — min/max timestamps actually staged per symbol and
  resolution, after timezone conversion and file writing.
- `effective_algorithm_window_ms` — what LEAN actually ran after applying the
  algorithm's `SetStartDate`/`SetEndDate` calls and config defaults.

For reconciliation-grade runs, those windows must align exactly, except for an
explicitly declared warmup/staging extension that is excluded from the
comparison window. Compatibility runs may allow a user-authored algorithm to
choose a narrower effective window, but the response must show the three windows
and warn when they differ.

Every run must also prove non-empty data consumption for each requested
subscription: `bars_consumed_by_symbol[symbol] > 0`. The Phase 1 trusted sample
does this with explicit algorithm instrumentation. Before Phase 4 exposes
arbitrary user source, Phase 3 must either identify a reliable LEAN artifact for
bar consumption or inject a sidecar-owned audit hook that cannot alter strategy
logic. If consumption cannot be proven, the run is not eligible for
reconciliation and the UI must not label it successful without a warning.

### LEAN quantization floor

LEAN equity data on disk stores prices at 1/10000 dollar precision. That is a
hard floor on price parity even when every other assumption matches. Phase 5
must not assert `atol=1e-9` on LEAN-ingested prices or fill prices. The minimum
documented floor for US equity bar/fill price comparison is `atol=0.0001,
rtol=0`, with any larger tolerance justified in the reconciliation report.

### Statistics parity scope

Reconciliation-grade Phase 5 targets orders, fills, positions, trade PnL, and
equity curve first. LEAN's aggregate statistics are version- and definition-
sensitive (annualization constant, sample vs population standard deviation,
benchmark selection, risk-free source, and drawdown convention). They are
reported as LEAN-native diagnostics until a separate statistics-parity fixture
pins each formula. Do not treat Sharpe/drawdown/beta deltas as engine bugs
without a statistic-level reference note.

### Normalized output parser

LEAN result artifacts are external-runner output. Treat them like external API
ingestion:

- Parse LEAN timestamps with explicit formats and an explicit source timezone
  (`America/New_York` for US equity market timestamps unless the artifact
  proves another exchange timezone).
- Convert immediately to `int64 ms UTC` for every normalized DTO, manifest row,
  and API response.
- Do not use `pd.to_datetime(...)` defaults, `DateTime.Parse(...)`, or any
  parser path that accepts naive timestamps without attaching the exchange
  timezone deliberately.
- Keep raw LEAN artifacts unchanged under `workspace/output/`; normalized
  artifacts live under `normalized/`.

The parser owns tests for representative LEAN order, trade, chart, and
statistics artifacts before Phase 4 exposes arbitrary user source.

### Reproducibility manifest

`manifest.json` is not just a request echo. For any run that may later be used
as audit evidence or reconciliation input, it records hashes of every input that
can affect output:

- submitted source hash and `algorithm_type_name`
- `config.json` hash
- pinned LEAN image digest
- launcher version/hash
- full staged data manifest: every bar zip hash, factor file hash, map file
  hash, market-hours database hash, and symbol-properties database hash
- request parameters and limits
- data adjustment policy
- data normalization mode
- brokerage/fill/fee policy
- starting capital and account currency
- subscription fill-forward policy
- requested, staged-data, and effective algorithm windows
- bars consumed per requested subscription
- normalized parser version/hash
- start/end timestamps as `int64 ms UTC`

Changing the pinned LEAN image digest, staged metadata databases, data
adjustment policy, data normalization mode, or parser version invalidates existing LEAN-vs-Engine
reconciliation fixtures. Regeneration follows the golden-fixture lifecycle in
`.claude/rules/numerical-rigor.md`: deliberate, documented, and never silent.
The determinism equivalence must hold on the fixture-generating host and in CI
before a fixture is promoted to a regression gate.

---

## Phase sequencing (delta from the original plan)

The original plan's six phases are retained, with these adjustments encoded by Phase 0:

- **Phase 1 (Runner spike)** — now includes (a) authoring the launcher service, (b) resolving the image digest, (c) proving the Windows/Podman topology and workspace path mapping, (d) confirming which of `--cap-drop=ALL` / `--read-only` / `--tmpfs` / non-root user / disk quota the LEAN image tolerates, (e) proving the LEAN data-folder contract with a price/timestamp round-trip fixture, (f) staging and hashing metadata databases, factor files, and map files for the trusted sample, (g) producing one end-to-end run on a hard-coded trusted Python algorithm (no user input yet), (h) re-running the same sample with the same inputs and asserting deterministic artifacts or documented equivalence within the quantization floor, (i) proving requested/staged/effective date-window alignment and non-empty bar consumption.
- **Phase 2 (Python API)** — unchanged in shape; the runner.py invocations go through the launcher socket/HTTP rather than spawning podman as a child process of the data-plane container.
- **Phase 3 — renamed *Container Execution Boundary + Fidelity Boundary*** — is the gating phase before any UI takes arbitrary user input. The UI from Phase 4 may exist earlier only with a hardcoded trusted sample algorithm (no `algorithm_source` field, no textarea). This phase also lands the normalized parser tests, manifest hashing, disk-cap enforcement, and explicit compatibility-vs-reconciliation run classification.
- **Phase 4 (Frontend LEAN Lab)** - is the first user-facing path. Phase 4a shipped the trusted-sample form, 4b the equity chart, **4c the custom-algorithm textarea** (server-side accept of `algorithm_source` on `POST /lean/runs/start`), **4d the run-history sidebar** (`GET /api/lean-sidecar/runs` + sidebar component), and **4e form rehydration on sidebar click** (`getManifest` repopulates symbol/window/cash so re-running a past run is a one-click → tweak → submit loop). From 4c onward, a successful run is possible from `/lean-lab` by pasting/editing the `QCAlgorithm`, configuring the run, clicking Run, and viewing results. The acceptance is unconditional on the API and gated by a UI toggle (defaults off → trusted sample). Developer CLI helpers may exist for tests or spikes only; they are never the product workflow.
- **Phase 5 (Reconciliation-grade samples)** is multi-PR. **5a** shipped the self-reconciler (`POST /api/lean-sidecar/runs/{id}/reconcile` compares any past run's recorded fees against `IbkrEquityCommissionModel`). **5b** ships the reconciliation-grade trusted-sample template (`template: "reconciliation"` on the run request → bundles a sample with explicit `SetBrokerageModel(BrokerageName.InteractiveBrokersBrokerage, AccountType.Margin)` and `brokerage_policy="interactive_brokers"` in the manifest). **5c** wires the reconciler results into the frontend and adds the LEAN-Lab-vs-Engine-Lab trade reconciler. The fee reconciler is decoupled from template choice — a default-template run produces a "many drift" report (informative: brokerage choice matters), a reconciliation-template run produces a clean report.
- **Phase 6** - unchanged in scope.

## Phase progress notes

Per-phase progress notes live in `docs/architecture/phases/`. Each
Phase X PR adds one new file there rather than appending to this
ADR — kills the merge-conflict class where every parallel PR wanted
to insert its section at the same anchor.

Phase 1 foundation (in PR order):

- [Phase 1a](phases/phase-1a.md)
- [Phase 1b](phases/phase-1b.md)
- [Phase 1c](phases/phase-1c.md)

Phase 5 follow-ups (newest first):

- [Phase 5f](phases/phase-5f.md) — determinism gate + zip mtime fix
- [Phase 5e](phases/phase-5e.md) — populate `bars_consumed_by_symbol`
- [Phase 5d](phases/phase-5d.md) — populate `staged_data_window_ms`
- [Phase 5c](phases/phase-5c.md) — synthetic minute-quote staging
- [Phase 5b](phases/phase-5b.md) — reconciliation-grade trusted-sample template
- [Phase 5a](phases/phase-5a.md) — self-reconciler against IBKR commission model

Phase 4 UI follow-ups (newest first):

- [Phase 4f](phases/phase-4f.md) — lean_error_categories on rehydrated runs
- [Phase 4e](phases/phase-4e.md) — form rehydration from manifest
- [Phase 4d](phases/phase-4d.md) — run-history sidebar
- [Phase 4c](phases/phase-4c.md) — accept arbitrary algorithm source


---

## Open questions Phase 1 resolves

1. **Image digest** — pin `quantconnect/lean` to a `sha256:...` and record it here.
2. **Windows launcher topology** — native Windows, WSL2/podman-machine, or launcher container. Record path mapping, socket access, and UID/GID behavior.
3. **Security flags viability** — does the LEAN image tolerate `--cap-drop=ALL`, `--read-only`, `--tmpfs`, and `--user <uid>`? If no, document the minimal accepted relaxation.
4. **Disk cap implementation** — launcher-side workspace-size monitor/kill path for the bind mount, with optional `--storage-opt` only as overlay defense-in-depth.
5. **`config.json` template** — capture the exact, working LEAN config keys against the pinned image.
6. **Data-folder fixture** — pin the exact minute zip/entry format, metadata database paths, factor/map file policy, and quantization tolerance in tests.
7. **C# viability** — prove `Main.cs` compiles under `--network=none`, or keep C# disabled in the UI.
8. **Determinism gate** — run the trusted sample twice with the same manifest inputs and assert byte-identical normalized output, or document the minimal accepted equivalence class before any fixture is trusted.
9. **Date-window / consumption gate** — prove the trusted sample's requested window, staged data window, and effective LEAN algorithm window align, and prove `bars_consumed_by_symbol` is non-zero. Decide the Phase 3 mechanism for arbitrary user algorithms.
10. **Per-run cost** — measure cold-start vs warm-start and decide whether to keep an idle warm runner pool in Phase 2+.

---

## Out of scope for this doc

- Persistence model for run metadata (file-backed vs DB) — decided in Phase 6.
- Reconciliation taxonomy specific to LEAN-Lab-vs-Engine-Lab — extends `.claude/rules/numerical-rigor.md` § "Trade-level reconciliation taxonomy" in Phase 5; not redefined here.
- Multi-tenant / multi-user concerns. learn-ai is single-operator; this design assumes one trusted user driving the UI.
- Live trading. LEAN Lab is research-only, matching the existing repo posture (`docs/references/lean-engine.md` § "What was NOT ported").

---

## References

- `docs/architecture/engine-authority-map.md` — engine ownership map. LEAN Lab is added there as an "external compatibility/reference runner" row in this Phase 0 PR.
- `docs/references/lean-engine.md` — what is vendored from LEAN and pinned (`references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/`). The sidecar runner is distinct from this vendored extract.
- `.claude/rules/numerical-rigor.md` — timestamp rigor and reconciliation taxonomy.
- `compose.yaml` — current container topology; `polygon-data-service` is the data-plane container.
- QuantConnect LEAN docs: https://www.quantconnect.com/docs/v2/lean-engine
- QuantConnect local data format/storage docs: https://www.quantconnect.com/docs/v2/lean-cli/datasets/format-and-storage
- QuantConnect local backtest docs: https://www.quantconnect.com/docs/v2/lean-cli/backtesting/deployment
