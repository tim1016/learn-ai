---
id: VCR-0011
severity: P1
status: remediated
area: untraced-bugs
canonical_file: PythonDataService/app/engine/live/host_daemon.py:905
reference: docs/architecture/adrs/0007-host-daemon-shared-secret-auth.md
first_seen: 2026-06-14
last_seen: 2026-06-14
regrounded_on: 2026-06-14
regrounded_to: high
phase_0_verdict: confirmed_valid
remediated_in: "Phase 7C — hmac.compare_digest in _verify_token + VCR-0011 prefix-rejection regression test"
lens: live-deploy-flow
dedupe_with_F: none
confidence: high
---

## Phase 0 re-grounding (2026-06-14)

**Verdict:** CONFIRMED VALID. Single site, 2-line mechanical fix.

**Evidence:**

- `_verify_token` lives at `PythonDataService/app/engine/live/host_daemon.py:902-909` (not `daemon_auth.py` as the original finding's `canonical_file` field assumed — corrected here).
- Line 905: `if supplied != token:` — plain string inequality. Vulnerable to timing-side-channel attack.
- Module does not import `hmac`. No constant-time compare anywhere in the daemon auth path.
- `_verify_token` gates every actuation route (`auth = [Depends(_verify_token)]` at line 911) — deploy, start, stop, emergency-flatten, instances, audit-copy-sizing. `/health` is the only unauthed route.

**Minimal fix:**

```python
import hmac

def _verify_token(...):
    if not hmac.compare_digest((supplied or "").encode("utf-8"), token.encode("utf-8")):
        raise HTTPException(...)
```

**Test:** smoke test that the comparison still succeeds/fails on equal/unequal tokens; timing-stability assertions optional.

## What

The host-daemon's shared-secret auth (ADR 0007) compares the supplied `X-Live-Runner-Token` to the configured token using a plain inequality (`supplied != token`) rather than a constant-time comparison (`hmac.compare_digest`). This is the standard timing-side-channel weakness for token-equality checks.

The blast radius is bounded by the daemon being bound to non-loopback (intentional per ADR 0007, so the data plane in the container can reach the launcher via `host.containers.internal`). The attack model is anyone who can probe response latency on the daemon's network surface — typically loopback / localhost / LAN, not the public internet. Even at that scope, ADR 0007's "shared-secret-on-every-request" contract is the entire defense in depth; the constant-time pattern is the convention.

## Where

- `PythonDataService/app/engine/live/daemon_auth.py` — `_verify_token` (or equivalent) plain-string comparison.
- `PythonDataService/app/engine/live/host_daemon.py` — `Depends(_verify_token)` on every actuation route except `/health`.
- `docs/architecture/adrs/0007-host-daemon-shared-secret-auth.md` — the contract this implements.

## Why this severity

PRD §7 P1: "UI implies guarantees the backend/runtime does not enforce" / "critical behavior without tests/provenance." Token-equality checks on the daemon's actuation surface are the single point of authorization for every deploy/start/stop/flatten command. The standard convention (constant-time compare) is universally adopted because timing channels are subtle and the cost of fixing them is one import.

Not P0 because no order or sizing math depends on a successful timing attack landing — the operator could detect the daemon being commanded externally via the bot control page instance list. But the auth surface should not be the soft target.

## Trading impact

- Speculative: an attacker on the same host (e.g., a different user account or a compromised local process) with the daemon's URL could probe the token character-by-character via response-time analysis. Networking constraints make this hard, but the threat model is non-zero.

## Reproduction

```bash
grep -nE '!= token|!= self\._token|!= settings\..*token' PythonDataService/app/engine/live/daemon_auth.py
# Confirm the comparison is plain string ==/!= and not hmac.compare_digest.
```

## Suggested resolution (NOT auto-applied)

Replace the comparison with `hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8"))`. Add a test asserting the comparison succeeds on equal tokens, fails on unequal tokens, and is timing-stable across the search space (the last assertion is a smoke test, not a deep statistical check — sufficient to prevent regressions to `==`).

## Provenance of the finding

Lens: `live-deploy-flow` (workflow `wf_def78013-ce4`). Lens summary identified the pattern; specific line in `daemon_auth.py` not re-verified by the main loop. `medium` confidence pending direct read. The standard fix is one line.
