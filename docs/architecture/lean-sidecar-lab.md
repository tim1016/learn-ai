# LEAN Sidecar Lab

**Status:** Phase 0 — architecture decision record
**Last reviewed:** 2026-05-17
**Pairs with:** `docs/architecture/engine-authority-map.md`, `docs/references/lean-engine.md`, `.claude/rules/numerical-rigor.md`

This document is the authority for the **LEAN Lab** feature — a UI surface in learn-ai where the user pastes or edits a real `QCAlgorithm` and runs it through an isolated official LEAN runner sidecar. It exists because the alternative ("just shell out to LEAN from FastAPI") quietly violates several invariants this repo depends on.

Phase 0 lands this doc and nothing else. Code starts in Phase 1.

---

## TL;DR

- LEAN Lab is a **sidecar reference runner**, not a new canonical engine.
- The Python Engine Lab (`app/engine/`) remains the canonical event-driven engine per `engine-authority-map.md`. That row does not change.
- User-supplied `QCAlgorithm` code executes **only** inside a disposable, network-isolated, capability-stripped LEAN container — never as a child process of `polygon-data-service`, the .NET backend, or Angular.
- The control-plane (whatever invokes `podman run`) is a separate small host-side launcher service. The data-plane container (`polygon-data-service`) never receives the podman socket.
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
| 6 | Resource limits are mandatory | `--cpus`, `--memory`, `--pids-limit`, and a wall-clock timeout are required parameters of the runner; tests assert the runner refuses to launch without them. |
| 7 | UI is the primary surface | `/lean-lab` page is the product path. CLI scripts exist only as developer-internal tools (e.g., the Phase 1 spike), are not packaged, and are not documented in the user-facing README. |
| 8 | All timestamps crossing the API boundary are `int64 ms UTC` | Per `.claude/rules/numerical-rigor.md` → "Timestamp rigor". Applies to request, response, and persisted manifest. |

---

## Container execution boundary

The non-negotiable shape of every LEAN sidecar invocation:

```
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

- `--read-only` — root filesystem read-only
- `--tmpfs /tmp:rw,noexec,nosuid,size=256m`
- `--user <non-root-uid>` — drop root inside the container

Phase 1's runner spike must record which of these three the LEAN image actually tolerates. If `--read-only` or non-root breaks LEAN, the doc gets updated and the workspace-only-mount + network-isolation + cap-drop + resource-limits set remains mandatory.

### Wall-clock timeout

The launcher enforces an outer timeout (kill the container) **in addition to** any LEAN-internal timeout. Defaults — verify on first run, then pin:

- Per-run timeout: **120s** (configurable in the request, capped at a server-side max).
- Log capture cap: **8 MB** truncated tail returned in the API; full logs persisted to the workspace.
- Per-run source-size cap: **256 KB** for `algorithm_source` (rejects oversized payloads at request validation).

---

## Workspace contract

Each run owns a fresh directory under the artifacts root. The data-plane container (`polygon-data-service`) writes into it; the launcher service mounts **only this directory** into the LEAN container.

```
PythonDataService/artifacts/lean-sidecar/<run_id>/
  workspace/                       # the only path mounted into the LEAN container
    project/
      main.py    | Main.cs         # user-submitted QCAlgorithm source
      config.json                  # LEAN config we author
    data/
      equity/usa/minute/<symbol>/  # LEAN-format zips from polygon_export.py
    output/                        # LEAN writes here
      logs.txt
      <statistics>.json
      <orders>.json
  normalized/                      # learn-ai DTOs parsed from output/
    result.json
  manifest.json                    # run_id, image digest, limits, request hash, timestamps (int64 ms UTC)
```

The repo root is never mounted into the LEAN container. The `data/` subtree is **pre-populated** by `polygon-data-service` before the runner is invoked; the LEAN container does no network I/O.

---

## Launcher topology — why a separate service

The data-plane container `polygon-data-service` runs inside podman (see `compose.yaml:43`). To launch a *sibling* LEAN container from inside it, there are exactly two options:

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **A.** Mount the podman socket into `polygon-data-service` | One less moving part | A successful exploit of any FastAPI handler that touches the socket = full host control. Directly violates non-negotiable #5. | **Rejected.** |
| **B.** Separate host-side **launcher service** that owns the socket | Privileged surface is one tiny service. `polygon-data-service` keeps its current attack surface. Launcher can refuse any path not under the artifacts root. | One new service to write, deploy, and authenticate. | **Chosen.** |

### Launcher shape (Phase 1 commits to specifics)

- Runs on the host (or in a privileged container with the podman socket mounted) — **not** inside `polygon-data-service`.
- Accepts a request over a **unix domain socket** bind-mounted into `polygon-data-service` (Linux/macOS) **or** localhost + shared-secret token (Windows dev hosts). The Windows token path is documented as the fallback and not used in any production-shaped deployment.
- Request payload is minimal — `{ run_id, image, limits }`. The launcher resolves `run_id` to a host-absolute workspace path itself using its configured artifacts root; the data plane never sends paths.
- Validates the resolved path is under the configured root, the workspace directory exists, and the requested image matches an allow-list of pinned digests.
- Invokes `podman run` with the flags in *Container execution boundary* above.
- Returns `{ exit_code, duration_ms, log_tail }` and persists full logs to `workspace/output/logs.txt`.

Phase 1 starts with the launcher running on the host directly (no container). A future hardening pass can move it into its own minimal container with **only** the podman socket and the artifacts root mounted.

---

## Runner choice — image, version, and the CLI question

The plan explicitly asks whether `lean backtest` (the official CLI from `lean-cli`) is acceptable. Decision:

- **Use the Docker image directly,** not the CLI, for the first implementation.
- Pinned image: `quantconnect/lean` at a specific digest resolved in Phase 1 and recorded here. The plan calls for `:latest` initially — that is acceptable for the Phase 1 spike, but the merged Phase 1 PR replaces `:latest` with a `sha256:...` digest in code and in this doc.
- The official `lean-cli` is a convenience wrapper that ultimately invokes the same image, and it adds account/auth steps and login state we don't want in a CI/server flow. Calling the image directly keeps the dependency surface to "podman + a pinned image digest".

### Config the launcher writes

The LEAN runner is driven by a `config.json` the data-plane authors and writes into `workspace/project/config.json`. The minimum fields LEAN needs to backtest from local data:

- `algorithm-language` (Python or C#)
- `algorithm-type-name` (entry class — defaults to `MyAlgorithm`; the data-plane parses the submitted source to confirm)
- `algorithm-location` (path inside the container — `/lean-run/project/main.py` or compiled assembly)
- `data-folder` — `/lean-run/data`
- `results-destination-folder` — `/lean-run/output`
- The LEAN parameters dict, populated from the request's `parameters` map

Phase 1 confirms the exact key names against the pinned image and records the canonical config template in this doc.

---

## Phase sequencing (delta from the original plan)

The original plan's six phases are retained, with these adjustments encoded by Phase 0:

- **Phase 1 (Runner spike)** — now includes (a) authoring the launcher service, (b) resolving the image digest, (c) confirming which of `--read-only` / `--tmpfs` / non-root user the LEAN image tolerates, (d) producing one end-to-end run on a hard-coded trusted sample algorithm (no user input yet).
- **Phase 2 (Python API)** — unchanged in shape; the runner.py invocations go through the launcher socket/HTTP rather than spawning podman as a child process of the data-plane container.
- **Phase 3 — renamed *Container Execution Boundary*** — is the gating phase before any UI takes arbitrary user input. The UI from Phase 4 may exist earlier only with a hardcoded trusted sample algorithm (no `algorithm_source` field, no textarea).
- **Phases 4–6** — unchanged in scope.

---

## Open questions Phase 1 resolves

1. **Image digest** — pin `quantconnect/lean` to a `sha256:...` and record it here.
2. **`--read-only` viability** — does the LEAN image work read-only with `--tmpfs /tmp`? If yes, mandate it. If no, document the writable surface and justify.
3. **Non-root execution** — does `--user <uid>` break LEAN's file-write paths? Record the answer.
4. **Launcher transport** — unix socket vs localhost+token: which the dev environment (Windows + podman) actually supports. The doc above prefers unix socket; Phase 1 may demote to localhost+token if podman-on-Windows blocks socket bind-mounts.
5. **`config.json` template** — capture the exact, working LEAN config keys against the pinned image.
6. **Per-run cost** — measure cold-start vs warm-start and decide whether to keep an idle warm runner pool in Phase 2+.

---

## Out of scope for this doc

- Persistence model for run metadata (file-backed vs DB) — decided in Phase 6.
- Reconciliation taxonomy specific to LEAN-Lab-vs-Engine-Lab — extends `.claude/rules/numerical-rigor.md` § "Trade-level reconciliation taxonomy" in Phase 5; not redefined here.
- Multi-tenant / multi-user concerns. learn-ai is single-operator; this design assumes one trusted user driving the UI.
- Live trading. LEAN Lab is research-only, matching the existing repo posture (`docs/references/lean-engine.md` § "What was NOT ported").

---

## References

- `docs/architecture/engine-authority-map.md` — engine ownership map. LEAN Lab is added there as an "external compatibility/reference runner" row in Phase 1's PR.
- `docs/references/lean-engine.md` — what is vendored from LEAN and pinned (`references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/`). The sidecar runner is distinct from this vendored extract.
- `.claude/rules/numerical-rigor.md` — timestamp rigor and reconciliation taxonomy.
- `compose.yaml` — current container topology; `polygon-data-service` is the data-plane container.
- QuantConnect LEAN docs: https://www.quantconnect.com/docs/v2/lean-engine
