# 247-Critical-Feedback — Phase 1 runner spike (1a + 1b)

**Branch:** `lean-sidecar/phase-1-runner-spike`
**Authored:** 2026-05-17 by Claude (Opus 4.7), autonomously while operator was away.
**Authority for the work:** `docs/architecture/lean-sidecar-lab.md` (PR #247).

**Phase 1b update (after image pulled):** Image digest pinned, security-flag matrix run, three end-to-end LEAN sidecar runs landed, `--cap-drop=ALL` promoted to mandatory in the runner. See "Phase 1b actual outcome" near the bottom of this doc. The original Phase 1a content below is preserved for the record of what I shipped before the image landed.

This document is for **your review when you return**. It lists every
non-trivial autonomous decision I made, the gaps that remain in
Phase 1, and the items where I'd specifically like your judgement
before Phase 1c lands.

## Where things stand

### Committed on this branch

- `cfed1282` — `chore(repo): gitignore *.lscache + sync date-window note in engine-authority-map`
- `1712cea1` — `feat(lean-sidecar): Phase 1 spike — launcher, runner, manifest, trusted sample`
- (about to commit) ADR Phase 1a progress note + this file

### Test posture

- **lean_sidecar package** — 59 unit tests passing locally; 7 skipped (auto-skip on hosts without the pinned LEAN image).
- **Full project test suite** — 2894 passed, 2 failed, 17 skipped, 5 xpassed.
  - **Both failures are pre-existing on master**, not caused by this PR. I verified by stashing my changes and re-running just those two — same failures.
    - `tests/research/ml/test_generator.py::test_cli_generates_real_artifact_for_one_day`
    - `tests/research/parity/test_qc_aapl_phase3_trade_parity.py::test_qc_aapl_phase3_trade_level_parity`
  - I have **not** investigated or filed issues for these. They were inherited tech debt and outside the scope you asked me to take on.
- **Project-scope lint** — `ruff check PythonDataService/app/ PythonDataService/tests/` is clean. CI-equivalent.

## The big gap — image was not pulled in time

`podman pull docker.io/quantconnect/lean:latest` was started early in the session and **never completed inside the working window**. I confirmed via a foreground attempt that real progress was being made — multiple `Copying blob sha256:...` lines streamed in 10s of foreground pulling — but the multi-GB transfer is genuinely slow on this host. When you return, the simplest thing to do is:

```powershell
podman pull docker.io/quantconnect/lean:latest
python PythonDataService/scripts/lean_sidecar_pin_image.py
```

The pin script will:

1. Resolve the registry digest via `podman image inspect ... --format '{{.Digest}}'`.
2. Rewrite `PINNED_LEAN_IMAGE_DIGEST` in `PythonDataService/app/lean_sidecar/config.py`.
3. Print the digest for the ADR.

You should then:

4. Update the `docs/architecture/lean-sidecar-lab.md` row that names the digest (in §"Runner choice" — the line that says "pinned image: `quantconnect/lean` at a specific digest resolved in Phase 1 and recorded here").
5. Run `pytest tests/lean_sidecar/test_security_flags.py tests/lean_sidecar/test_runner_e2e.py -v` — both auto-run as soon as the image is present.
6. Use whatever the security-flag matrix outcome is to update §"Container execution boundary" — specifically the `--cap-drop=ALL` / `--read-only` / `--tmpfs` / `--user` rows.
7. Commit all of this as Phase 1b in the same PR (or a fast-follow).

I deliberately did **not** start the pull as a never-ending background loop or schedule a self-poller, because the cycle to validate that you actually want this image pulled is short once you're back, and the unattended pull may have died for environmental reasons I cannot debug from inside this session.

## Decisions I made autonomously — please flag any you'd reverse

### A. Scope: Phase 1a, not all of Phase 1

I bit off the subset of Phase 1 (a, e, and partial g+i) that does not require the pulled image, plus the writer/reader fidelity proof that does not require a live container. I explicitly deferred (b, c, d, f, g full, h, plus reconciliation-grade extras) into Phase 1b. The ADR §"Phase 1a progress" section is the load-bearing record of what's shipped vs queued.

If you wanted me to wait for the image pull and ship a single complete Phase 1, this is the place to push back.

### B. Launcher language and topology

- **Python + FastAPI**, separate package at `PythonDataService/app/lean_sidecar/launcher/`. Pure module + thin FastAPI app so tests call `launch()` in-process without binding a TCP port.
- **Launcher runs on the host, not inside `polygon-data-service`.** The data-plane container can reach it on `localhost:8090`; `LEAN_LAUNCHER_TOKEN` env var optionally adds a shared-secret header.
- **The launcher does not yet have its own Docker image / compose entry.** It's a Python module a developer can run directly with `uvicorn`. I did not want to commit to a compose change without your eyes on it, because the ADR explicitly defers "launcher in its own container" hardening to a later pass.

If you want the launcher to be a separate container right now (rather than after Phase 1b), say so and I'll wire it up — it's a 20-minute change but it touches `compose.yaml` which I treated as load-bearing infra.

### C. Trusted sample is buy-and-hold SPY, not an indicator strategy

I picked the most trivial deterministic algorithm I could. The test value is: "does the sandbox round-trip data, run an algorithm, and produce output". A more elaborate strategy adds testing surface without buying anything for Phase 1 — and per ADR §"Statistics parity scope", aggregate statistics are NOT in scope yet.

### D. Tolerance choices

- **Round-trip fidelity test:** asserts byte-exact deci-cent encoding (`int(price * 10000)`). The ADR's `atol=0.0001` floor is the price-comparison tolerance for runs that pass through LEAN; the *writer/reader* contract is integer-exact, and the test reflects that.
- **WindowMs validation:** rejects zero-length and reversed windows up-front. Catches a class of "we have a date but no data" silent-empty bugs before they reach the manifest.
- **No tolerance loosening anywhere.** All assertions are at the strictest default.

### E. Manifest fields chosen up-front, even though Phase 1 doesn't populate all of them

`RunManifest` already has fields for `effective_algorithm_window_ms`, `bars_consumed_by_symbol`, `started_at_ms`, etc., even though Phase 1a only populates a subset. They are `None` / empty by default. This means Phase 2's parser can fill them without bumping `MANIFEST_SCHEMA_VERSION` — additive only. The alternative (add them as Phase 2 grows) would have rotated all Phase 1b reconciliation fixtures the moment Phase 2 lands, which is the opposite of what `numerical-rigor.md` wants.

### F. `--user <non-root-uid>` is **xfailed**, not asserted

The security-flag matrix test does not hard-fail on `--user`. The LEAN image historically runs as root; if a future image starts shipping a non-root user, the xfail will promote to xpass and we can tighten the assertion. Hard-asserting now would block any Phase 1b run.

### G. I did not touch authority docs other than the ADR

`engine-authority-map.md` got the trivial "date-window" carry-forward (from your queued local edit). `math-sources-of-truth.md` was NOT touched — the trusted-sample doesn't add new canonical math. `numerical-rigor.md` was NOT touched — Phase 5 is where the LEAN-Lab-specific reconciliation taxonomy extension lands, per ADR §"Out of scope for this doc".

### H. Stale `.claude/launch.json` modification

When I ran `git stash` to verify the test failures were pre-existing, an older stash that was already in your repo (containing a "Frontend (Worktree 4201)" launch config) got auto-popped. I left it in your working tree, **not** committed. Take a look — if it's yours, commit it separately on a different branch.

## Things I think are wrong with the ADR (small)

Two minor things I noticed reading the doc carefully while writing the code, that I'd flag for a future doc-tidy pass but did not edit:

- §"Container execution boundary" lists `--cap-drop=ALL` in the "mandatory non-conditional" shape, then immediately under "if compatible" says "currently listed in the required shape above; Phase 1 verifies the image tolerates it. If not, the runner must use the smallest documented capability allow-list and this ADR must be updated in the same PR." This is internally consistent but slightly self-contradictory at first read — the flag is "mandatory subject to image tolerance". Phase 1b should rewrite this section as either "promoted by matrix to mandatory" or "removed because the image refuses" — pick one.
- §"Launcher shape" promises a unix domain socket on Linux/macOS and shared-secret on Windows. Phase 1a only implements the localhost+token path. The unix-socket support is straightforward to add but introduces transport branching in the FastAPI layer; I deferred it.

## What I'd ask for explicitly when you're back

1. **Approve or amend the Phase 1a/1b/1c scope split** so I know whether Phase 1c is "everything else" or a curated subset.
2. **Decide whether the launcher gets its own container in Phase 1c** or stays as a host process for now.
3. **Tell me whether to baseline the 2 pre-existing test failures** as a separate cleanup ticket, or whether you already track them somewhere.

Everything else I'm happy to keep deciding as I go — flag the ones you want bubbled up to you.

— Claude

---

## Phase 1b actual outcome (after the image landed)

When the pull completed, I ran the full Phase 1b plan from this doc. Everything below was decided autonomously; flag anything you'd reverse.

### Image pinned

`sha256:97884667be20077925996ac22b5e3e16e3a47e7363e01795151459d16786247c` — written into `PythonDataService/app/lean_sidecar/config.py` by `scripts/lean_sidecar_pin_image.py` and echoed into the ADR top + §"Phase 1b progress".

### Security-flag matrix outcome

Tested at two levels: podman-startup (the `test_security_flags.py` `echo` smoke) and LEAN-runtime (E2E variants in `test_runner_e2e.py`). Outcome table:

| Flag | Podman | LEAN runtime | Status |
|---|---|---|---|
| `--cap-drop=ALL` | accepted | clean | **Promoted to mandatory** in `runner.py` |
| `--pids-limit=512` | accepted | clean | Already mandatory |
| `--tmpfs /tmp:rw,noexec,nosuid,size=256m` | accepted | not yet tested in a full E2E | Caller opt-in |
| `--read-only` | accepted | breaks Algorithm.Initialize | **Deferred to Phase 1c** (LEAN's ObjectStore defaults to `/Lean/Launcher/bin/Debug/storage`, on the read-only overlay) |
| `--user 10001:10001` | accepted | not yet tested in a full E2E | **Deferred to Phase 1c** (workspace UID/GID on Windows + WSL2 not pinned) |

The "podman accepts but LEAN runtime breaks" finding for `--read-only` is captured as an `xfail` in `test_buy_and_hold_runs_with_read_only_root` with the diagnostic in the test docstring, so the next person who tries to promote `--read-only` will see exactly what to fix.

### Three bugs found and fixed during Phase 1b

1. **LEAN ran the wrong algorithm.** First clean E2E run completed with exit-code 0 but produced no output. The launcher's `podman run quantconnect/lean` used the image-baked `config.json` (which selects `BasicTemplateFrameworkAlgorithm`) instead of mine. Fix: `runner.py` now always appends `--config /lean-run/project/config.json` to the LEAN launcher args. Without this, every "successful" run silently executes the wrong code.
2. **`bar.EndTime.ToUniversalTime()` is .NET, not Python.** QC's Python bridge passes a naive `datetime.datetime` in algorithm timezone. Fix: trusted sample now imports `zoneinfo`, attaches ET, and converts via `.timestamp()`.
3. **LEAN's post-run ResultsAnalyzer needs benchmark data.** Default benchmark is SPY daily; my trusted sample only stages SPY minute. LEAN crashed in `ResultsAnalyzer.ReadEquityCurve`. Fix: `MyAlgorithm` now pins `SetBenchmark(lambda dt: 100)` to a constant.

### New helper: extract metadata from the LEAN image

LEAN refuses to initialize without `symbol-properties-database.csv` and `market-hours-database.json`. These ship inside the image at `/Lean/Data/`. Added `stage_lean_metadata_from_image(workspace, image_digest)` that uses `podman create + podman cp + podman rm` (no `run`, no network) to extract them into the workspace before launch. Manifest hashing then covers exactly the bytes LEAN reads.

### Test posture after Phase 1b

- **lean_sidecar package** — 67 tests passing, 1 xfail (the documented `--read-only` regression). 0 skipped — every test that gated on `requires_lean_image` now runs.
- **Full project test suite** — not re-run after Phase 1b changes; the only file outside `lean_sidecar/` I touched in Phase 1b is the ADR. Will run before mark-ready-for-review.
- **Project-scope lint** — clean.

### Phase 1c queue (smaller than the original Phase 1b queue)

1. Pin a non-root UID/GID for the workspace, then enable `--user <uid>` mandatory.
2. Either add an `ObjectStore` tmpfs or override `object-store-root` in `config.json`, then enable `--read-only` mandatory.
3. Add the determinism re-run check (trivial now that one clean E2E run exists — same `run_id` with same inputs should produce byte-identical artifacts modulo timestamps).
4. Test `--tmpfs /tmp:rw,...` at full LEAN runtime; promote to mandatory if clean.
5. Wire the unix-domain-socket transport in the launcher (currently only localhost + optional token).

---

## Phase 1c actual outcome (review-driven, same PR)

Three reviewer-flagged blockers landed before merge — none was caught by the prior unit suite or by my own real-HTTP test, all three matter for "Phase 1 actually delivers what it claims":

### Clean-run classification beyond exit code

Real bug: a launch that crashed `ResultsAnalyzer`, failed several `SubscriptionDataSource` reads, *and* hit a missing-symbol-properties error STILL returned exit_code 0. The Phase 1b assertion `assert response.exit_code == 0` was a lying success signal.

Fix: new `app/lean_sidecar/result_classifier.py` parses LEAN's `output/log.txt` after every run and buckets `ERROR::` lines into four stable categories — `analysis_failed`, `failed_data_requests`, `runtime_error`, `other`. `LaunchResponse` now exposes `lean_errors: dict[str, list[str]]` and a top-level `is_clean: bool`. `is_clean` is True iff exit code is 0, the run did not time out, AND the classified-error dict is empty. The trusted-sample E2E test asserts no error category appears beyond an explicit `_TRUSTED_SAMPLE_KNOWN_NOISE` allow-list (currently just `_quote.zip` — see "trusted sample is not reconciliation-grade" below).

New tests: `tests/lean_sidecar/test_result_classifier.py` (9 cases). Representative log shapes harvested from real Phase 1b LEAN runs so the classifier is exercised against shapes I've actually seen, not synthetic ones.

### `observations.csv` visibility — bar-consumption gate (i)

Real bug: LEAN's `ObjectStore` defaults to `/Lean/Launcher/bin/Debug/storage` which is on the image's overlay. The trusted sample's `observations.csv` was being written but to a path I'd never see — meaning the ADR's bar-consumption gate was technically un-satisfied even though I claimed otherwise.

Fix: `LeanConfig` now sets `object-store-root` to `/lean-run/output/storage` (a workspace path). `Workspace.object_store_dir` exposes it. The E2E test asserts the file exists with a non-trivial body (`ms_utc,close` header + at least one bar row).

### Explicit handling of failed data requests

Real bug: LEAN's default minute subscription requests Trade *and* Quote bars; the post-run analyzer needs SPY daily for the equity curve; `InterestRateProvider` needs `data/alternative/interest-rate/usa/interest-rate.csv`; `LocalDiskMapFileProvider` warns when `map_files/` is missing. The trusted sample triggered all of them, and Phase 1b logged them as a generic "log_tail" string without classification.

Fix: stage what we can, document the rest as known noise:

- `stage_daily_bars()` — writes one synthetic daily bar per trading day from the last minute close, eliminating both the `daily/spy.zip` not-found and the `ResultsAnalyzer` equity-curve crash.
- `stage_lean_metadata_from_image()` extended to also extract `/Lean/Data/alternative/interest-rate/` into `workspace/data/alternative/interest-rate/`.
- `stage_empty_corporate_action_dirs()` — empty `factor_files/` + `map_files/`; the trusted-sample window has no corporate actions so empty is the right semantic.
- Quote-bar staging is Phase 5+ work. Until then the test allows ONE documented noise pattern (`_quote.zip` not-found) and treats any other unexpected error as a regression.

### Smaller improvements that came with the same review

- **Hardening flags structural validation.** `--tmpfs` without a following spec is rejected before podman sees it. New tests cover the bad-pair shapes.
- **`launcher.log` shell-quoted single-line form.** Added `# shell: podman run --rm …` to the plan header so an operator can copy/paste to reproduce manually. The argv-per-line audit form stays for grep-ability.
- **Trusted sample explicitly non-reconciliation-grade.** Docstring now leads with the boundary; the test helper is named `_assert_trusted_sample_run`, not `_assert_clean_run`, so the distinction is visible at every call site.

### Tests after Phase 1c

90 passed, 1 skipped (Windows symlink). No xfail (the Phase 1b `--read-only` xfail was kept but the trusted-sample test now uses `_assert_trusted_sample_run`, which the read-only test also calls — so the xfail still gates on the same `Read-only file system` substring in the log tail).

### What this changes for Phase 2+

Phase 2's parser can now read `lean_errors` from the response and `observations.csv` from the workspace without any further wiring; the contract surface is stable. The Phase 5 reconciliation work will replace `_assert_trusted_sample_run` with a strict `_assert_reconciliation_grade_run` that requires `response.is_clean is True` AND `lean_errors == {}` AND a proper benchmark + brokerage pin in the algorithm. Those two helpers will sit side by side in the test module so the contrast between compatibility-grade and reconciliation-grade is one short read.

