> **Status:** Archived evidence (2026-07-22).
> **Do not use as implementation authority or an operator procedure.**
> **Current authority:** `docs/bot-control-operator-manual.md`, ADR-0030, and ADR-0026.
> **Archived because:** This dated cohort attempt is retained only as validation history.

# Paper three-bot cohort attempt — 2026-07-16

## Outcome

**Blocked; no validation certificate was issued.** The intended paper-only
cohort could not legally complete its T+0 / T+15m / T+30m schedule plus the
required 15-minute healthy-overlap proof before the RTH forced-flat boundary.
This attempt is not evidence that three bots ran continuously and healthily.

## Intended cohort

- `lease-val-20260716-a4` — run `4d0c8257aee545283c4721d8028f62d880052cde8f684abbad48bd44f4d0efc8`
- `lease-val-20260716-b4` — run `5794f64e459eb1dee6a64cc8457dc9d2dce08efff6c7931be8af5e2fc6ac7533`
- `lease-val-20260716-c4` — run `a09b13afe1903dde019096ac8547725be308e19442d6af3022e9376cc3f38de0`

All operations were performed through the operator browser surface. No bot
start, stop, or flatten endpoint was actuated by a script.

## What happened

1. A first browser launch did not produce the required staggered validation
   proof and is excluded from this cohort result.
2. The browser's paper emergency-flatten control was used. The subsequent
   account surface showed no positions and no open orders. Its final account
   verdict was `not_proven` because no active Clerk remained, so this is a
   flat-account observation, not a replacement for the missing certificate.
3. The legacy roster entries were retired through the browser. Fresh roll call
   offers for the three intended members were issued at approximately 14:32–
   14:33 Central and expired at `2026-07-16T19:55:00Z` (14:55 Central / 15:55
   New York).
4. After that expiry, the browser's Ready view contained no eligible members.
   The configured default RTH effective stop was already reached; the NYSE
   regular session had also closed.

## Durable blocker and correction

The V2 stagger profile previously admitted a cohort while only checking that
each individual start was before its own stop. It did not prove that the whole
cohort window could fit before the earliest persisted member stop. The server
now rejects the authorization before writing a cohort receipt or starting Bot
A when:

`now + 30 minutes + 5 seconds + 15 minutes > earliest effective stop`

For the default 15:55 New York forced-flat boundary, the latest admissible
start is 14:09:55 Central (15:09:55 New York). Operators should begin well
before that time to leave operational margin.

The guard reads every selected bot's immutable `live_config`, fails closed
when that policy is unreadable, and returns
`COHORT_WINDOW_EXCEEDS_SESSION_STOP` with both the effective stop and required
window end. It does not alter session policy, force-flat time, or any safety
artifact.

## Evidence and verification

- Unit and HTTP-boundary tests prove that a 14:10 Central request is rejected
  before any authorization event or bot start.
- Focused Python validation passed: 233 tests.
- Project-wide Ruff validation passed for `PythonDataService/app/` and
  `PythonDataService/tests/`.
- A full Python-suite attempt was stopped after the local terminal wrapper
  duplicated the runner; it was not used as a passing result.

## Next legal execution

On the next RTH session, verify the account remains flat in the browser, issue
a fresh roll call, select only the three intended bot instances, and authorize
the `paper_three_bot_stagger_v2` profile before the cutoff. Preserve the
resulting server-authored cohort receipt and certificate; do not reuse today's
expired offers or edit durable safety artifacts.
