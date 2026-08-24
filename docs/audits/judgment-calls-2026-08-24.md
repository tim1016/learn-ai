# Judgment calls — 2026-08-24 (afternoon lifecycle + validation-fleet session)

Operator directive: "stop these bots, pause them, resume them, try launching
deployment-validation strategy bots, document any failures, gather data toward
a practical/simple/robust/scalable launch system" — executed autonomously,
with the calls below. Morning calls (window choice, SPY-only fleet, engine
divergences non-blocking) are in
`paper-ceremony-strategy-fleet-2026-08-24.md` §4 and are not repeated here.

1. **Circuit order: stop → resume per bot, pause as a probe.** The literal
   instruction was "stop, pause, resume", but a stopped bot cannot be paused,
   and reconnaissance showed `pause` is never presented under SQLite custody
   (study F2). I ran stop→resume per bot, captured the pause refusal as
   evidence instead of skipping it silently, and ended with the fleet
   running — the end state the instruction implies.

2. **Panel actions as the primary surface, legacy stop on exactly one bot.**
   The presented-actions API is the receipted, revision-guarded surface the UI
   uses, so it carries the study's weight; one bot went through the legacy
   runner stop deliberately to measure the two parallel stop paths against
   each other (60× latency gap, study F3). Sequential, not parallel, per bot —
   cleaner latency data and no clerk contention risk while measuring.

3. **Fixed the Resume token bug on master immediately instead of only
   documenting it.** The bug stranded all five bots OFF_DUTY with no
   working Resume (UI included), directly blocking the operator's "resume
   them". Master commits were pre-authorized today; the fix follows the
   module's own documented pattern for the identical 2026-08-04 bug; the
   regression test was written first and failed pre-fix. Committed as
   `238821c7`.

4. **Restarted the data-plane service while the whole fleet was stopped.**
   Hot reload is broken on this stack, so loading the fix required a restart.
   Doing it at the moment every bot was stopped and flat made the restart
   free of orphaned-process risk. The ~45 s feed-readiness cold start it
   exposed became study finding F4 rather than an incident.

5. **"Deployment validation strategy bots" read as the `deployment_validation`
   registry strategy, plus `ema_crossover_signal`.** `deployment_validation`
   is the platform's purpose-built validated qualification strategy (used by
   the prior SQLite campaigns), and ema is the only other strategy with an
   accepted proof — including it exercised the pairing-already-active launch
   path for contrast. Three bots: SPY + QQQ on deployment_validation (QQQ
   adds a multi-symbol scalability data point), SPY on ema. All safe_canary
   1-share, mode=paper.

6. **Ran negative probes against the production paper account.** The
   override-on-accepted probe was fired twice (before pairing, after
   pairing) to capture both refusal gates. Judged safe: a 409 deploys
   nothing, creates no bot, and the probe instance ids are throwaway. The
   second probe exists because the first proved only the selectability gate
   (study F5) — reporting it as proof of the override boundary would have
   been wrong.

7. **Left all 8 bots running through the close.** The morning directive
   ("make them run through the day") still stands and the afternoon exercise
   ends in the running state; the re-armed monitor watches all 8 until
   16:00 ET, when session results complete the companion audit's §6.

8. **Commits stay local to master, unpushed.** Same call as the morning:
   the operator's evening review is the push gate. Three code/doc commits
   today so far (`02365e82`, `20338171`, `238821c7`) plus the documents.

9. **No thermo review for `238821c7`.** The thermo gate is defined as
   one-shot per PR at first push; today's work is direct-to-master by
   operator instruction with no PR yet. If these commits are later bundled
   into a PR, thermo runs once before that push per the standing rule.
