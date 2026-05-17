# 247-Critical-Feedback — Phase 1a runner spike

**Branch:** `lean-sidecar/phase-1-runner-spike`
**Authored:** 2026-05-17 by Claude (Opus 4.7), autonomously while operator was away.
**Authority for the work:** `docs/architecture/lean-sidecar-lab.md` (PR #247).

This document is for **your review when you return**. It lists every
non-trivial autonomous decision I made, the gaps that remain in
Phase 1, and the items where I'd specifically like your judgement
before Phase 1b lands.

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

1. **Approve or amend the Phase 1a scope split** so I know whether Phase 1b is "everything else" or a curated subset.
2. **Decide whether the launcher gets its own container in Phase 1b** or stays as a host process for now.
3. **Tell me whether to baseline the 2 pre-existing test failures** as a separate cleanup ticket, or whether you already track them somewhere.

Everything else I'm happy to keep deciding as I go — flag the ones you want bubbled up to you.

— Claude
