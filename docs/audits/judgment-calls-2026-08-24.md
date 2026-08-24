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

Afternoon probe round (operator: "keep tinkering, find bugs; launch more
deployment-validation bots"):

10. **Did not force-qualify NVDA/IWM.** Their refusals are the golden
    qualification corpus working as designed (seal covers SPY/QQQ/TSLA/AAPL).
    Extending the corpus is a qualification job plus a new seal — a
    deliberate promotion task, not an afternoon workaround. The fleet covers
    all four qualified symbols instead.
11. **Probes ran against the production paper account.** All probes are
    refusal-expected or read-only except two throwaway bots (a race winner,
    stopped immediately; a dry-run attempt that never registered). Judged
    safe: a 409/422 deploys nothing, and the paper account is the ceremony
    surface the operator is exercising anyway.
12. **Dry-run orphan directories moved aside, not deleted.** The failed
    dry-run probes left `sim:<sid>/` authority dirs; they were quarantined to
    `_probe_orphans_2026-08-24/` following the platform's move-aside-never-
    delete custody philosophy, even for synthetic probe garbage.
13. **F10–F16 documented, not fixed, today.** Unlike the Resume token bug
    (which stranded the operator's explicit request), the afternoon findings
    are topology decisions (dry-run authority placement), design questions
    (idempotency scope, retire), or performance work (read fan-out) — each
    needs a deliberate owner decision, not a mid-session patch.
14. **Scheduled a supervised crash-recovery test for ~14:40 ET.** Restarting
    the data plane with 10 running bots exercises the resume-after-crash
    path (duty outcome honesty, crash-candidate recreation, carryover
    refusals) with worst-case exposure of a few 1-share paper positions, and
    leaves the 15:45 strategy flatten barrier plus an hour of session as the
    recovery margin.
15. **Fixed the order_ref-cap bug at the deploy boundary immediately, on
    master.** Same bar as the Resume token fix: an organic crash
    (`OrderRefTooLongError`) with a dormant, purpose-built guard already in
    the codebase and zero callers — wiring it in is the module's own
    documented intent, shipped with a pre-failing regression test
    (`ff5ed49f`). The three surviving long-named ceremony bots were stopped
    (they could never trade); only crashed Strategy C got a replacement
    (`cer-c-0824`) this late in the session — B/RSI/SMA replacements would
    have had ≤40 minutes of 15-minute-bar session left, so their redeploy
    is left for the next session.
16. **Left the three crash-held 1-share positions stranded-but-honest.**
    Every recovery pointer (resume refusal, carryover copy,
    prepare_safe_flatten, manual tickets) leads to a flatten that does not
    exist on this stack (study F18). The two possible unblocks — presenting
    `flatten_stop` under SQLite custody, or building the SafeFlattenPlan
    executor — are custody-policy changes an owner should review, not an
    end-of-session patch; and closing the positions directly at Alpaca would
    manufacture foreign SELLs the clerk would rightly flag. Three 1-share
    paper positions are the cheapest possible standing evidence of the gap.
17. **SIGKILL, not graceful restart, for the crash test** — a crash test
    that lets shutdown hooks run isn't testing a crash. The same restart
    doubled as the loader for the boundary fix.
