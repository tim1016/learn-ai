# Session handoff — 2026-05-18, post-Phase-5g code review

**Purpose:** carry the post-Phase-5g code-review work across a session
boundary. Read this first if you're picking up where 2026-05-18 left
off.

## Where master is

Phase 5g is **feature-complete + production-runnable end-to-end**.
The end-to-end browser smoke test (LEAN-Lab trusted run → cross-engine
reconcile) was verified live in this session — it produced a real
report with one `quantity_mismatch` (gating) row and one
`commission_drift` (diagnostic) row, exactly the design intent.

Last merged master commit: PR #277 (review hotfix batch 1).
**Five additional PRs are in flight** from this session — verify CI
and reviewer-comment state before doing anything else.

## PRs in flight (verify status first)

| # | Branch | Scope |
|---|--------|-------|
| #278 | `lean-sidecar/p1-2-reject-reused-run-id` | P1.2 — orchestrator rejects reused `run_id` with HTTP 409 |
| #279 | `lean-sidecar/p1-3-failure-manifest` | P1.3 — always write a manifest, even on launcher failure (audit gap) |
| #280 | `lean-sidecar/p1-1-container-kill` | P1.1 — `--cidfile` + `podman stop`/`rm` on wall-clock timeout |
| #281 | `lean-sidecar/q1-q2-token-and-assert-fees-guard` | Q1 mandatory launcher token + Q2 `assert_fees` IBKR-brokerage guard |

All four are independent — they touch different files and can land in
any order. Each PR's body has its own "Independence" note where
relevant.

**Before merging any of them**, run:

```bash
gh pr view <N> --json mergeStateStatus,statusCheckRollup --jq '{state: .mergeStateStatus, running: [.statusCheckRollup[]? | select(.status == "IN_PROGRESS") | .name], failed: [.statusCheckRollup[]? | select(.conclusion == "FAILURE") | .name]}'
gh pr view <N> --json reviews,reviewDecision
```

Address any P1 reviewer comments before merge (per repo memory).
CodeRabbit + Codex both review automatically; if either flags a Major
or P1, fix in-PR before merge.

## Remaining work from the post-Phase-5g review

Two items are intentionally left unscoped — they need design choices
**you** should make, not autonomous picks. Each has its own
design-handoff document:

- **P1.4 — live workspace cap** → see
  `docs/handoffs/2026-05-18-design-p1-4-live-workspace-cap.md`
- **P2.5 — date semantics** → see
  `docs/handoffs/2026-05-18-design-p2-5-date-semantics.md`

**When to use Claude (design):** these two items involve API-shape and
runtime-architecture tradeoffs. Don't ask a regular implementation
agent to pick — they'll pick whatever feels safest and you'll inherit
the wrong tradeoff. Use Claude (design) — Opus 4.7 in design mode, or
the `Plan` / `feature-dev:code-architect` subagent — to walk through
the tradeoffs first, lock in your choice, **then** hand to an
implementation agent for the actual PR.

The design-handoff docs are self-contained: each lists the options
with effort + tradeoffs, my recommended approach, the relevant files,
and the test surface to expect. Read either one in isolation and
design without re-reading the rest of this handoff.

## Mission-critical doc — current state of D-conditions

`docs/architecture/lean-sidecar-mission-critical.md` is the authority
for autonomous decision boundaries. Current state:

- **D1** multi-symbol observation schema — defer
- **D2** determinism gate tolerance — defer
- **D3** Phase 5g cross-engine reconciler — **resolved**, shipped
- **D4** Phase 6 SQLite persistence — defer until cross-run query
  use-case surfaces
- **D5** real data vendor — defer; Massive.com noted as candidate when
  re-scoped (Polygon Starter is free-tier equivalent)
- **D6** algorithm class-name configurability — **resolved**, keep
  `MyAlgorithm` hardcoded
- **D7** equity-chart spec flake — **resolved**, spec deleted
- **D8** CodeRabbit Major policy — **resolved**, case-by-case
- **D9** dev deps — **resolved**, allowed if widely-used + pinned
- **D10** reconciler schema_version — **resolved**, shape changes
  allowed with `schema_version` bump

No new D-conditions added this session. The two design handoffs
(P1.4, P2.5) explicitly call out where I'm declining to make the call
autonomously.

## Environment gotchas (carry to the next session)

These bit me this session; flagging so future sessions don't re-learn:

- **Container can't reach host via `host.containers.internal`** on
  this podman-machine install — it resolves to a non-routable
  link-local. `compose.yaml` already hardcodes
  `LEAN_LAUNCHER_URL=http://172.23.176.1:8090` (the WSL2 adapter IP);
  override in `.env` if your adapter differs. PR #276 added this; if
  the launcher process is restarted on a fresh boot the IP may
  change — `ipconfig | findstr WSL` to discover.
- **`restart.sh` does NOT restart the host launcher** — the launcher
  is a host Python process (`python -m uvicorn
  app.lean_sidecar.launcher.app:app --host 0.0.0.0 --port 8090`),
  separate from compose. Restart it manually when launcher-code
  changes need to take effect.
- **`podman cp ... container:/app/...` writes through bind mounts to
  the host filesystem.** Multiple times this session I copied a file
  INTO the data-service container and it appeared on the host repo as
  a "leak". Stash + drop pattern works (`git stash push -m
  "podman-cp-leaks" <files>; git pull; git stash drop`).
- **Test runs in the container need pytest installed at every
  rebuild.** Image doesn't bake it in.
  `podman exec polygon-data-service pip install pytest pytest-asyncio
  respx pytest-httpx` if collection fails.
- **`test_router_lean_sidecar.py` has an autouse fixture** that
  `delenv`s `LEAN_LAUNCHER_URL` + `LEAN_LAUNCHER_TOKEN` so respx
  mocks against `DEFAULT_LAUNCHER_URL` intercept. Don't remove it.

## Suggested next-session order

1. Verify and merge the 4 in-flight PRs (#278, #279, #280, #281).
2. Spawn Claude (design) on the P1.4 handoff. Pick the workspace-cap
   approach. Hand to implementation.
3. Spawn Claude (design) on the P2.5 handoff. Pick the date-semantics
   approach. Hand to implementation. (Bigger semantic shift — likely
   wants more discussion than P1.4.)
4. After both design tickets land, run the end-to-end browser smoke
   test again to confirm no regression.

## Repo-state snapshot

- Branch: `master` at PR #277 + four open PRs above
- Worktrees alive (clean up after merge):
  - `learn-ai-p11/` (PR #280)
  - `learn-ai-p12/` (PR #278)
  - `learn-ai-p13/` (PR #279)
  - `learn-ai-q1q2/` (PR #281)
  - `learn-ai-handoff/` (this PR)
- Three lingering podman-cp leak files in main worktree (stash + pull
  drops them when each PR merges).
