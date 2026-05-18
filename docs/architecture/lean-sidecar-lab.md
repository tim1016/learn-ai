# LEAN Sidecar Lab

**Status:** Phase 1 — runner spike shipped (Phase 1a + 1b)
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
| 12 | LEAN output parsing is a timestamp ingestion boundary | LEAN's naive result timestamps are parsed as exchange-local timestamps with explicit formats, then converted to `int64 ms UTC` before API response or persistence. |
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
- **Phase 5 (Reconciliation-grade samples)** is multi-PR. **5a** ships the self-reconciler (`POST /runs/{id}/reconcile` compares any past run's recorded fees against `IbkrEquityCommissionModel`). **5b** adds the reconciliation-grade trusted-sample template with explicit IBKR brokerage pinning. **5c** wires the reconciler results into the frontend and adds the LEAN-Lab-vs-Engine-Lab trade reconciler. The reconciler is decoupled from template choice — a default-brokerage run produces a "many drift" report that's informative (it shows brokerage choice matters), not a bug.
- **Phase 6** - unchanged in scope.

### Phase 1a progress (2026-05-17)

Shipped in PR following Phase 0:

- (a) **Launcher service authored** — `PythonDataService/app/lean_sidecar/launcher/`. Pydantic request model enforces digest pin + run-id slug + limit positivity. The service writes the planned `podman run` argv to `workspace/launcher/launcher.log` *before* spawning so an audit trail survives a launcher crash. `LaunchRejectedError.reason` is a stable label (`"workspace_not_staged"`, `"runner_configuration_error"`, `"invalid_run_id_or_path"`) for caller-side routing without parsing free text.
- (a, cont.) **Workspace path-under-root contract** — `app/lean_sidecar/workspace.py`. `run_id` is a strict slug (`^[a-z0-9][a-z0-9_-]{2,63}$`); resolution rejects symlink escapes; layout creation is idempotent.
- (e) **LEAN data-folder fidelity proof** — `tests/lean_sidecar/test_data_folder_fidelity.py` (7 cases). Asserts deci-cent round-trip (the integer disk encoding is exactly `price * 10000`), ET timestamp normalization (UTC inputs serialize to the equivalent ET ms-since-midnight), canonical zip layout (`equity/usa/minute/<sym>/<YYYYMMDD>_trade.zip`), and the LEAN quantization floor at `0.0001` for the smallest representable price.
- **Manifest contract** — `app/lean_sidecar/manifest.py`. All `int64 ms UTC`; the serializer refuses `datetime` objects at the boundary; atomic temp+rename write; sorted-pretty JSON so the file hash is stable across Python dict-iteration changes.
- **Trusted Python sample** — `app/lean_sidecar/trusted_samples/buy_and_hold.py`. Class is `MyAlgorithm` (matches the ADR's documented default). `SetCash` is explicit, `fillForward=False`, `DataNormalizationMode.Raw` — the reconciliation-grade defaults from §"Fill-forward policy" and §"Data normalization mode policy" are wired in from the start so the sample is reconciliation-eligible without a future rewrite.
- **`config.json` authoring** — `app/lean_sidecar/lean_config.py`. Container-side paths hard-coded against the `/lean-run` mount; sorted-pretty JSON for stable hashing. Phase 1 confirms the exact key names against the pinned image (see Open Questions §5).
- **Test surface** — 59 unit tests passing; security-flag matrix + E2E sidecar test gated on the locally-pulled LEAN image (skip-with-clear-reason on hosts that have not pulled it).

### Phase 1b progress (2026-05-17, same PR)

After Phase 1a landed, the LEAN image pull completed; Phase 1b added:

- (b) **Image digest pinned.** `sha256:97884667be20077925996ac22b5e3e16e3a47e7363e01795151459d16786247c` (see top of doc). `PINNED_LEAN_IMAGE_DIGEST` in `app/lean_sidecar/config.py` is the source of truth; `scripts/lean_sidecar_pin_image.py` writes it from `podman image inspect`.
- (c) **Windows topology — provisional.** Launcher is a host Python process invoking podman over the WSL2/podman-machine VM. Workspace bind-mounted via the standard WSL2 path translation. UID/GID matching for `--user` is a Phase 1c fast-follow (see security matrix below). The "launcher in its own container with only the Podman socket mounted" hardening pass remains deferred.
- (d) **Security-flag viability matrix (Phase 1b run on the pinned digest):**

  | Flag                                       | Podman-startup | LEAN-runtime  | Status                          |
  |--------------------------------------------|----------------|---------------|---------------------------------|
  | `--cap-drop=ALL`                           | ✅ accepted    | ✅ ran clean  | **Mandatory** in `runner.py`    |
  | `--pids-limit=512`                         | ✅ accepted    | ✅ ran clean  | **Mandatory** in `runner.py`    |
  | `--read-only`                              | ✅ accepted    | ✅ ran clean  | **Mandatory** (Phase 1c — see note below) |
  | `--user=<dynamic>`                         | ✅ accepted    | ✅ ran clean  | **Mandatory** (Phase 1c — dynamic UID, see note below) |
  | `--tmpfs /tmp:rw,noexec,nosuid,size=256m`  | ✅ accepted    | ➖ untested   | Opt-in (caller passes flag)     |

  Phase 1b initially deferred `--read-only` and `--user`; Phase 1c
  promoted both to mandatory after the trusted-sample E2E proved them
  viable at full LEAN runtime. `--read-only` works because Phase 1c's
  `object-store-root` config override moved LEAN's ObjectStore out of
  the image overlay (`/Lean/Launcher/bin/Debug/storage`) into
  `/lean-run/output/storage` — a workspace-writable path under the
  single bind mount. `--user` resolves dynamically via
  `runner._container_user_spec()`: on Linux the container's UID/GID
  matches the launcher's `os.getuid()`/`os.getgid()` so the container
  can write to launcher-created workspace files (without the dynamic
  match, native Linux hosts hit POSIX permission errors on
  `workspace/output` writes); on Windows + WSL2 where `os.getuid`
  doesn't exist the helper returns `10001:10001` as a non-root
  fallback that works because the WSL2 mount layer doesn't enforce
  host POSIX ownership inside the container.

- (f) **Metadata staging from image.** Added `stage_lean_metadata_from_image(workspace, image_digest)` in `app/lean_sidecar/staging.py`. Uses `podman create` + `podman cp` (no run, no network) to extract `/Lean/Data/market-hours/market-hours-database.json` and `/Lean/Data/symbol-properties/symbol-properties-database.csv` into the workspace's `data/` subtree. The launcher then mounts only the workspace; LEAN reads the metadata from a hashable path under the audit boundary instead of from the image-baked defaults.
- (g) **End-to-end trusted-sample run.** Three tests in `tests/lean_sidecar/test_runner_e2e.py`:
  - `test_buy_and_hold_runs_clean` — baseline shape, **passes**.
  - `test_buy_and_hold_runs_with_cap_drop_all` — adds `--cap-drop=ALL`, **passes** at full LEAN runtime.
  - `test_buy_and_hold_runs_with_read_only_root` — adds `--read-only + tmpfs /tmp`, **xfails** with the ObjectStore message captured in the test docstring.
- (i, partial) **Bar-consumption audit file.** The trusted sample writes `observations.csv` to LEAN's `ObjectStore` recording `(ms_utc, close)` for every received bar; the Phase 2 parser will read this and assert non-zero consumption + the three-window alignment.

Two trusted-sample fixes also landed in Phase 1b:

- LEAN's launcher reads `config.json` from its working directory by default (which is the image-baked default config pointing at `BasicTemplateFrameworkAlgorithm`). The runner now always appends `--config /lean-run/project/config.json` as the launcher arg so the workspace config wins; this is the safety floor noted in `runner.py:CONTAINER_LEAN_CONFIG_PATH`.
- `bar.EndTime` arrives as a naive Python `datetime` in algorithm timezone (ET), not a wrapped .NET `DateTime`; the sample now attaches ET via `zoneinfo` before converting to int64 ms UTC.
- `SetBenchmark(lambda dt: 100)` pins a constant benchmark so LEAN's post-run `ResultsAnalyzer` does not try to read SPY daily data that the trusted-sample-window does not stage.

### Phase 1c progress (2026-05-17, same PR — review-driven hardening)

After Phase 1b, reviewer feedback flagged three blockers for *claiming*
fidelity / reconciliation-readiness even from the spike. They land in
this same PR before merge:

- **Clean-run classification beyond exit code.** Exit-code 0 was lying — LEAN can crash `ResultsAnalyzer`, fail `SubscriptionDataSource` reads, or raise in `Algorithm.Initialize` while still exiting 0 (Phase 1b shipped that bug; Phase 1c catches it). New `app/lean_sidecar/result_classifier.py` parses `output/log.txt` after every run and buckets `ERROR::` lines into four stable categories: `analysis_failed`, `failed_data_requests`, `runtime_error`, `other`. `LaunchResponse` now carries `lean_errors: dict[category, list[str]]` and a top-level `is_clean: bool` that is True only when exit code is 0, the run did not time out, AND the classified-error dict is empty. Test surface: `tests/lean_sidecar/test_result_classifier.py` (9 cases including representative log shapes harvested from real Phase 1b runs).
- **`observations.csv` visibility (bar-consumption gate (i)).** The trusted sample writes through LEAN's `ObjectStore`, which previously rooted at `/Lean/Launcher/bin/Debug/storage` (image overlay) — invisible to the manifest and unwritable under `--read-only`. `LeanConfig` now sets `object-store-root` to `/lean-run/output/storage` (a workspace path), `Workspace.object_store_dir` exposes it, and the E2E test asserts the audit file lands there with a non-trivial body. Until this PR, "bar-consumption inspectable" was a Phase 1 claim the code did not actually deliver.
- **Explicit handling of failed data requests.** LEAN's default minute subscription also requests quote bars; the post-run `ResultsAnalyzer` needs SPY daily for benchmark equity-curve; `InterestRateProvider` needs `data/alternative/interest-rate/usa/interest-rate.csv`; `LocalDiskMapFileProvider` warns when `map_files/` is missing. Phase 1c addresses them by:
  * staging a synthetic daily SPY bar per trading day (`stage_daily_bars`),
  * extending `stage_lean_metadata_from_image` to extract the bundled `/Lean/Data/alternative/interest-rate/` subtree alongside `market-hours` + `symbol-properties`,
  * creating empty `factor_files/` + `map_files/` directories (`stage_empty_corporate_action_dirs`) — the trusted-sample window has no corporate actions so empty is the right semantic.
  After Phase 1c, the only LEAN log noise the trusted sample emits is `_quote.zip` not found (LEAN's minute subscription requests Trade *and* Quote bars; staging quotes is Phase 5+ work). The E2E test asserts that *no* category other than this documented known-noise pattern appears.

Three smaller hardening items also landed:

- **Hardening flags allow-list with structural validation.** The `runner.ALLOWED_HARDENING_TOKENS` allow-list now rejects unknown tokens AND verifies that paired flags (e.g., `--tmpfs <spec>`) have a value token after the flag name. `extra_image_args` is removed from `LaunchRequest` entirely; callers cannot tack on post-image flags.
- **Post-run `workspace_max_mb` enforcement.** The launcher walks the workspace after `execute()` and raises `LaunchRejectedError("workspace_max_mb_exceeded", …)` if the cap was overrun. Symlinks are not followed. Live mid-run monitoring is the Phase 1c+ ADR item.
- **`launcher.log` operator-friendly form.** The plan header now writes a shell-quoted single-line form (`# shell: podman run --rm …`) in addition to the argv-per-line audit form.

### Trusted-sample reconciliation status

The trusted sample is **not reconciliation-grade**, by construction:

- `SetBenchmark(lambda dt: 100)` pins a constant benchmark so the post-run `ResultsAnalyzer` does not need market-cap benchmark data the sample does not stage.
- Brokerage / fill / commission models are LEAN defaults.
- Only five trading days of synthetic minute bars; no factor or map files; no quote bars.

Reconciliation-grade samples (Phase 5) will:

- Stage real benchmark daily data and remove the `SetBenchmark` hack.
- Pin Interactive Brokers brokerage and the documented fill / fee models per ADR §"Brokerage, fill, and fee policy".
- Stage factor / map files for any window that touches a corporate action.
- Stage quote bars for any algorithm that consumes quotes.

This boundary is captured in `buy_and_hold.py`'s docstring; the E2E test calls `_assert_trusted_sample_run` not `_assert_reconciliation_grade_run` (the latter is a Phase 5 fixture).

Open from this PR, queued for Phase 1d / Phase 5:

- `--user <uid>` requires workspace UID/GID matching on Windows + WSL2 — launcher does not pin a UID yet.
- `--read-only` is now safe-er with ObjectStore inside the workspace, but `/Lean/Launcher/bin/Debug/` paths LEAN still touches for storage need a tmpfs; not promoted to mandatory.
- **Determinism gate** — re-run + byte-identical normalized-artifact comparison. Trivial to add now that the clean-run contract is enforced; deferred so this PR does not grow further.
- Quote-bar staging — eliminates the last known-noise category in the trusted-sample log.
- Real factor/map files for the reconciliation-grade Phase 5 fixtures (not for the spike).
- Hardening-profile enum to replace caller-supplied `hardening_flags` argv tokens — reviewer-suggested longer-term direction.

### Phase 5a progress (2026-05-17, follow-up PR — self-reconciler against IBKR commission model)

First slice of the reconciliation-grade work. Ships the primitive that
every other Phase 5 deliverable depends on: a categorized comparison
of recorded fees against the canonical `IbkrEquityCommissionModel`
(which has lived in `app/research/parity/ibkr_commission.py` since
Engine Lab's QC reconciler work — Phase 5a consumes it without
duplicating the formula).

- **API** — `POST /api/lean-sidecar/runs/{id}/reconcile` returns
  `RunReconciliationReportModel { run_id, algorithm_id,
  total_fill_events, matched_count, divergent_count, commission_atol,
  total_recorded_fees, total_expected_ibkr_fees, divergences[] }`.
  Reads the normalized result.json for the run, walks filled events,
  computes the expected IBKR fee per event, classifies each as clean
  / `commission_drift` / `no_recorded_fee`. Tolerance is the
  numerical-rigor.md default ($0.01).
- **Reconciler module** — `app/lean_sidecar/reconciler.py` is pure
  functions over `NormalizedOrderEvent` iterables. Three exports:
  `FeeDivergenceCategory`, `FeeReconciliationReport`,
  `reconcile_against_ibkr`. The categories are a strict subset of the
  project-wide `DivergenceCategory` so consumers can lift them into
  the broader taxonomy without translation.
- **Decoupled from template choice.** A trusted-sample run that used
  LEAN's default brokerage will produce a report full of
  `commission_drift` rows — that's *expected* and informative (it
  shows the brokerage choice matters). The clean-vs-drift signal only
  becomes interpretable as "Engine-Lab-comparable" once the Phase 5b
  reconciliation-grade template pins IBKR brokerage explicitly.
- **Decimal hygiene on the wire.** All money values cross the API as
  strings (not floats) so JSON serialization is exact. The reconciler
  quantizes both recorded and expected fees to cents internally so the
  $0.01 tolerance is meaningful at the cent boundary.
- **What 5a does NOT do** — does not modify any run, does not include
  the reconciliation-grade template (Phase 5b), does not surface the
  report in the UI (Phase 5c), does not handle quote bars / factor
  files / benchmark staging (separate Phase 5b+ work items).
- **Test surface** — 19 unit tests on the pure reconciler (empty list,
  non-filled events excluded, status case insensitivity, clean run,
  drift detection, no-recorded-fee categorization, tolerance boundary,
  aggregate totals, negative quantity, custom atol, custom model, edge
  cases: zero qty, percentage cap, parametrized boundary classification);
  5 endpoint integration tests (clean run, drift surface, 404 on missing
  workspace, 404 on missing normalized, invalid run_id rejection).
  199 lean_sidecar tests pass + 1 skip.

### Phase 4e progress (2026-05-17, follow-up PR — form rehydration from manifest)

Phase 4d added the sidebar but a click only repopulated the result
panel; the form fields stayed at their defaults, so re-running a past
configuration meant re-typing it. Phase 4e closes that loop.

- **No new endpoint.** `GET /api/lean-sidecar/runs/{id}/manifest`
  has existed since Phase 2a; Phase 4e just adds a typed frontend
  wrapper (`LeanSidecarService.getManifest`) that returns a narrow
  `RunManifest` TS interface — only the fields the form needs are
  typed; the rest of the dict passes through as `unknown`.
- **`rehydrateFormFromManifest` policy.** Symbol, starting cash,
  and the requested window come from `manifest.parameters` and
  `manifest.requested_window_ms`. The algorithm source is NOT
  rehydrated — the manifest stores only its sha256 (provenance hash),
  not the source itself. The toggle resets to off; operators re-running
  a user-source algorithm re-paste it. A fresh `runId` is generated
  so a re-run with the rehydrated form lands in a new workspace
  (mixing artifacts in the same dir would corrupt the audit trail).
- **Defensive wire-type coercion.** `starting_cash` is serialized as
  a string by the trusted-sample staging code and as a number
  elsewhere; the rehydrator accepts both. Out-of-range cash (below
  the $1k server min) is rejected from the patch entirely rather
  than auto-clamped — patching it in would immediately invalidate
  the form, and the operator is better served seeing the old value
  with a fresh symbol/window than seeing the form go red on click.
- **Manifest fetch failure is non-fatal.** A 404 (legacy run with
  no manifest written) leaves the form at its previous values; the
  result panel still renders. A swallow-with-comment is the right
  call here — surfacing every 404 in the UI for the legacy-run case
  would be noise without a remediation action.
- **Test surface** — 4 new component specs (full rehydration, numeric
  starting_cash variant, 404 leaves form intact, below-min cash
  rejected from patch); 2 new service specs (getManifest success,
  getManifest 404 envelope). 39 frontend tests pass (was 33).

### Phase 4d progress (2026-05-17, follow-up PR — run-history sidebar)

After Phase 4c made arbitrary user source first-class, the page still
forgot every run the moment the operator submitted the next one or
refreshed the browser. Phase 4d adds the missing read-side affordance:
a sidebar that lists past runs and lets the operator click one to
rehydrate it in the main panel. No persistence change: the index is
built by scanning the artifacts root on demand.

- **API** — `GET /api/lean-sidecar/runs` returns `RunIndexResponse {
  runs, cap, truncated }`. The scan reads each `<artifacts_root>/<run_id>/manifest.json`,
  extracts a compact `RunSummaryModel` (run_id, symbol, requested
  window, started/finished ms, exit_code, `algorithm_source_kind`,
  `exit_clean`), and sorts by `started_at_ms` desc. Capped at 200
  rows so a pathological artifacts root cannot balloon the response.
  Pure read — does not touch the launcher, does not require LEAN to
  be running. Half-written or non-JSON manifests are silently skipped
  so a crash mid-write does not break the listing.
- **Slug-pattern filter at the directory boundary.** The scan only
  enumerates directories whose names pass `RUN_ID_PATTERN`, so a
  stray out-of-band tar extract (`/artifacts/Not a Slug!/manifest.json`)
  never reaches the response — the sidebar is not a free file-browser.
- **UI** — new `LeanLabRunHistoryComponent` (presentational): takes
  `runs`, `selectedRunId`, `loading`, `truncated` as inputs and emits
  `runSelected: string`. Renders a colored status dot per row (green
  for `exit_clean=true`, red for `false`, grey for null/no manifest)
  plus a "custom" tag when `algorithm_source_kind="user_provided"`.
  Parent `LeanLabComponent` owns the run-list signal, refreshes it on
  init + after every successful submit, and handles click by calling
  `getNormalized()` and rehydrating the main panel. The form fields
  are intentionally NOT repopulated on click — keeping the form
  primed for the next submit is the lower-surprise behavior. Form
  rehydration from manifest is a Phase 4e candidate.
- **`exit_clean` is intentionally weaker than `is_clean`.** The
  manifest doesn't store `lean_errors` so the index can't reconstruct
  the full clean signal (exit==0 AND no LEAN errors AND not timed
  out). The sidebar uses `exit_clean` only for at-a-glance row color;
  clicking a row still rehydrates the normalized result, which is
  where the operator gets the real picture.
- **Test surface** — 8 new router tests (sort order, manifest-missing
  skip, corrupt-manifest skip, non-slug skip, summary-field
  extraction, exit-clean false branch, legacy-manifest unknown-kind,
  empty root). 7 new component specs for the standalone sidebar
  (empty state, row render, truncated banner, custom-tag rendering,
  click emits, click disabled while loading, aria-current on
  selected). 5 new specs on the parent component for the integration
  (init load, submit re-refresh, loadRun rehydration, loadRun 404
  surfaces error envelope, listRuns rejection survives gracefully).
- **What Phase 4d does NOT do** — does not introduce a database; the
  scan-on-demand approach is fine at 200 rows but will need an index
  cache + a real persistence layer for the Phase 6 multi-thousand-run
  case. Does not stream live progress for in-flight runs (the index
  only shows manifest-written runs, so an in-progress run appears at
  the top only after completion).

### Phase 4c progress (2026-05-17, follow-up PR — accept arbitrary algorithm source)

After Phase 1c promoted `--read-only` and `--user=<non-root>` to mandatory sandbox flags, the
`POST /lean/runs/start` API and the LEAN Lab UI both accept arbitrary `QCAlgorithm` source from
the operator. Phase 1c's hardening is the precondition that makes accepting arbitrary source
acceptable.

- **API** — `TrustedRunRequestModel.algorithm_source: str | None`. Empty/whitespace is rejected
  with HTTP 422 (better signal than a silent fallback). UTF-8 size validated against
  `MAX_ALGORITHM_SOURCE_BYTES = 256 KiB`. Omitting the field falls back to the bundled trusted
  `buy_and_hold.py`. `extra="forbid"` still rejects unknown fields.
- **Service** — `TrustedRunRequest.algorithm_source` flows through to `stage_algorithm_source(...)`;
  the manifest gains `algorithm_source_kind={user_provided|trusted_sample}` so the audit trail
  records intent. Class name MUST be `MyAlgorithm` (matches `algorithm-type-name` in
  `LeanConfig`) — a mismatch causes LEAN to run its image-baked default and the run looks
  "successful" with empty output.
- **UI** — `lean-lab.component`: new Reactive Forms controls `useCustomAlgorithm: boolean` +
  `algorithmSource: string`. The toggle defaults off (operator still gets a one-click trusted
  run); turning it on reveals a monospace textarea pre-populated with a minimal `MyAlgorithm`
  template that runs against the sample data window. Whitespace-only source is silently omitted
  client-side rather than sent for a server 422.
- **Sandbox guarantee surfaced in UI copy** — header explicitly names the Phase 1c shape
  (read-only root, non-root user, no caps, no network, workspace-only mount) so the operator
  knows what protects the host when they paste arbitrary code.
- **Test surface** — 4 new router tests (`test_algorithm_source_optional`,
  `..._empty_string_rejected`, `..._oversize_rejected`, `..._within_cap_accepted`); 1 new
  field-rejection test renamed from the stale Phase 2a `test_forbids_algorithm_source_field`.
  3 new component specs cover (a) toggle-off omits the field, (b) toggle-on with source sends
  it, (c) toggle-on with whitespace-only omits it.
- **What Phase 4c does NOT do** — does not run arbitrary algorithms through the
  reconciliation-grade path; does not stage additional brokerage / fill model variants
  (still LEAN defaults); does not relax the trusted-sample data window or stage real factor
  / map files. Those remain Phase 5+.

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
