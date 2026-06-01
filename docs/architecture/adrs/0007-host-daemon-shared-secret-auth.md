# ADR 0007 — The host live-run daemon authenticates with a shared-secret token, which lets it bind a non-loopback interface so the containerized data plane can reach it on Linux podman

**Status:** Accepted 2026-05-31.
**Decision drivers:** On Linux rootless podman the entire daemon-backed live-instances UI (deploy, Start/Stop, the "engine up" check) is dead in-container: the daemon enforced a loopback-only bind (it was unauthenticated), and `host.containers.internal` does not reach host loopback on Linux. The previous attempt to "just bind 0.0.0.0" is rejected by the daemon's own validator and would expose an unauthenticated deploy/trading control plane on the LAN.
**Related:** ADR 0003 (host-venv topology), ADR 0004 (instance-addressed control plane), ADR 0006 (deploy control-plane host-daemon), `app/lean_sidecar/launcher_auth.py` (the precedent this mirrors), the deploy-form picker (`broker-deploy-form`).

## Context

The host live-run daemon (`app/engine/live/host_daemon.py`) owns the git clean-tree check, run-id minting (`/deploy`), and the live-trading subprocesses (`/runs/{id}/start|stop`). It runs as a host process (ADR 0003: IBKR error 420 forces the bot to co-locate with Gateway on the host). The containerized data plane (`polygon-data-service`) forwards to it (ADR 0006) via `settings.live_runner_daemon_url` → `http://host.containers.internal:8765`.

Two facts collided:

1. **The daemon was unauthenticated, so it bound loopback only.** `_loopback_host` rejected any non-loopback `--host`. The runbook called it "a local operator bridge only."
2. **A loopback bind is unreachable from the container on Linux.** `host.containers.internal` forwards to host loopback on Windows/Mac podman (gvproxy), but on Linux rootless podman it maps to the bridge gateway (the host's LAN IP). A loopback-bound daemon does not answer there — verified: container → `host.containers.internal:8765` returns `curl` exit 7, and the data plane fails closed to an empty listing, surfacing as the deploy form's empty "Algorithm audit copy" picker (the symptom that opened this investigation).

The data plane's "fail closed to empty" is deliberate (ADR 0006 §3 error pattern), so the gap presents as *missing data*, not an error — making it easy to misread as "nothing committed."

The LEAN sidecar launcher (`app/lean_sidecar/launcher_auth.py`) already solved the identical problem for a different host process: it binds `0.0.0.0` and gates every capability behind a mandatory `X-Launcher-Token` shared secret, auto-generated to a bind-mounted file the data plane reads. Its own history records why opt-in auth is unacceptable: "opt-in token means no auth in practice."

## Decision

### 1. The daemon authenticates every protected route with a mandatory shared-secret token

`X-Live-Runner-Token` is required on every route except `/health` (kept open so the connectivity probe works without a token; it leaks no capability — only process liveness). A missing/wrong token returns `401`. There is **no unauthenticated mode**: the token is resolved or generated at startup, mirroring the launcher. `auth_token=None` to `create_app` means "resolve/generate," not "open"; tests opt into a known token explicitly.

### 2. The token is auto-generated to a bind-mounted file, with an env override

Resolution order (`daemon_auth.ensure_daemon_token` / `read_daemon_token`): `LIVE_RUNNER_DAEMON_TOKEN` env → `<artifacts-root>/.host-daemon-token` file → freshly-generated `secrets.token_urlsafe(32)` written `0o600`, atomically. The file lives at the artifacts root (sibling to `live_runs/`), which both the host daemon and the container view through the existing `./PythonDataService/artifacts:/app/artifacts` mount — so no manual secret-sync and no compose change are needed for the default path. Operators who prefer an explicit secret set the env var on both processes.

`daemon_auth.py` deliberately **mirrors** `lean_sidecar/launcher_auth.py` rather than importing it: the host daemon must not depend on the lean-sidecar subsystem. The two host services keep parallel, independent token files. (Considered and rejected: extracting a shared generic token module — it would refactor the working launcher for a ~40-line saving, against the standing preference to keep scope narrow.)

### 3. With auth mandatory, the loopback-only bind restriction is lifted

`--host` now accepts any valid IP (or `localhost`); garbage still fails fast. The default stays `127.0.0.1` (correct on Windows/Mac, where the container reaches loopback). **Linux operators set `--host 0.0.0.0`** so the container's gateway hop reaches the daemon — now safe because every capability is token-gated. The systemd installer keeps a loopback default with `HOST` overridable.

## Consequences

**Positive:**
- The live-instances UI works in-container on Linux podman (set the daemon to `0.0.0.0`), unblocking the deploy/Start/Stop flow that ADR 0006 assumed would forward cleanly.
- Binding a non-loopback interface no longer exposes an unauthenticated control plane — the security property the loopback guard stood in for is now enforced directly, and on any interface.
- Mirrors the launcher's proven pattern, so the two host services are consistent for operators and reviewers.

**Negative / costs:**
- Direct operator `curl` against the daemon now needs the token (read it from the token file or set the env). The runbook is updated.
- A second auto-generated secret file under `artifacts/`. Documented; `0o600`; hidden name.
- Modest, deliberate duplication of the token bootstrap across the two host services (see §2).

**Follow-ups (not in scope):**
- A `/diagnose`-style self-test for the daemon hop (the launcher has one); for now a `401` or empty picker plus the connectivity strip is the signal.
- Removing the `host.containers.internal:host-gateway` override in favour of pasta host-loopback on Linux would be an alternative to `0.0.0.0`; not pursued here.
