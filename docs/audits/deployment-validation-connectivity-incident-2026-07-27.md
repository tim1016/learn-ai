# Deployment Validation: the day the fleet lost its horizon

**Incident date:** 2026-07-27

**Report boundary:** 14:27 CT, when the operator reported Internet loss and IB Gateway unavailability

**Account:** `DUM284968` - PAPER

**Experiment:** independent Deployment Validation bots on SPY, QQQ, AAPL, MSFT, NVDA, AMD, AMZN, and META

**Outcome:** safe stop before the connectivity loss; experiment objectives incomplete
**Evidence basis:** run ledgers, lifecycle dispositions, Account Clerk journal, account reconciliation receipt, registry, UI observations, and local Git history

## Executive summary

Fourteen paper-bot runs were launched in two waves. The first wave contained six bots. It exposed a serious ownership defect: a new bot could import sibling account-net positions as though they were its own. The fleet was stopped, the math-free ownership boundary was repaired to use Clerk-attributed positions, and all six runs were retired. A repaired wave of eight bots was then launched independently.

The eight-bot wave reached eight concurrent On-duty bots for about four minutes, not the required fifteen. It completed ten strategy round trips before an AMD order exposed a second account-wide defect. AMD's submission waited behind the serialized Account Clerk queue. The caller's 30-second end-to-end deadline expired, AMD halted with the outcome marked uncertain, and the already durable buy was acknowledged and filled nine seconds after the process ended. The account safety gate then halted the seven sibling bots. This was a correct fail-closed response to an unknown order outcome.

Two shares required operator recovery: one META share was closed through exact namespace recovery, and the late AMD share was closed through the paper account's confirmed emergency flatten. Broker reconciliation subsequently proved the account clean, flat, and free of working orders. Two journal attribution adjustments then made the durable local ownership projection agree with that flat broker state.

At approximately 14:27 CT, after all experiment runs had been retired, the operator reported an Internet outage and an unavailable IB Gateway. No third wave was launched. The browser was left on an unsubmitted AMD replacement form. The last current broker proof was therefore safe, but it is now historical:

- At 14:25:32 CT, receipt `acct-recon-DUM284968-1785180332714-fea6adc105274768` proved PAPER account `DUM284968`, state `CLEAN`, flat exposure, no positions, no working orders, and a passing account reconciliation gate.
- At 14:26:05 CT, Clerk journal sequence 1841 completed the final ownership adjustment. A read-only projection then produced empty net positions, empty residual exposure, verdict `clean`, and `policy_blocks_starts=false`.
- All fourteen run records are `RETIRED` in the instance registry.
- The Clerk generation file reached generation 80 / `accepting` at 14:27:29 CT. That proves only the local Clerk process phase. It does not prove post-outage broker connectivity.
- Current broker truth is **unverified while offline**. The panel must not continue displaying its last green state as though it were live.

The elegant end-state is not merely "reconnect harder." It is a durable control plane with an explicit **Known / Stale / Unknown** truth model. On disconnect, bots and the UI should freeze order-capable actions, snapshot the last verified state, preserve unknown submissions by identity, and require a complete reconnect reconciliation before admitting any new start or submit.

## What the Bot Control panel was left holding

| Surface | Last durable or observed state | What it means after disconnect |
| --- | --- | --- |
| Fleet roster | Eight strategy instances off roster; fourteen run bindings retired | No experiment bot should still be order-capable |
| Broker account | Last verified PAPER, Clean, Flat, 0 positions, 0 working orders | Safe last-known state; not a claim about current broker truth |
| Ownership projection | Net `{}`, explained `{}`, residual `{}`, verdict `clean` | Durable local journal is internally balanced |
| Account Clerk | Generation 80, phase `accepting` in the local generation record | Local process state only; broker reachability is unknown |
| Browser | AMD replacement Deploy form, inherited posture `FLAT`, positions `{}`, pending orders `0` | A draft ticket only; **Deploy & run was not clicked** |
| Third-wave validation | Not started | The queue-timeout, cross-runtime lock, zero-holding, and sick-bay UI repairs remain code/test validated but not fully revalidated by a fresh live-paper fleet |

The panel should have changed immediately to an unmissable state such as:

> OFFLINE - BROKER TRUTH UNKNOWN
>
> Last verified 14:25:32 CT: PAPER / CLEAN / FLAT / 0 working orders.
> All starts and submits are locked. Reconnect reconciliation is required.

It should not say merely "Connected," "Clean," or "Flat" once the supporting evidence has exceeded its freshness boundary.

## The bots tell their own lives

The stories below use Clerk-recorded fills for order truth and lifecycle dispositions for terminal truth. Every strategy order used one fixed share in the paper account.

### Wave one - six lives that found the ownership defect

#### SPY, first life - run `a6901134...`

> I was created at 11:40:34 CT and started one second later. I was told to trade one SPY share whenever Deployment Validation changed state. I completed four full journeys: 738.42 to 738.25, 736.52 to 736.31, 736.62 to 735.96, and 736.71 to 736.26. At 12:25:27 the host ended me with process code -9 while the fleet was being withdrawn for repair. I could not write a normal exit status, so the registry called my ending `EXITED_UNVERIFIED`. I was explicitly retired at 12:37:37 and included in the final retirement transition at 14:12:09. I left no net SPY position.

#### QQQ, first life - run `997b036e...`

> I was deployed at 11:43:27 CT. An operator stopped me flat during an investigation, then discovered that the panel showed Start even though my durable STOPPED latch required Resume. The UI was repaired; I was resumed and started again at 11:52:20. I then completed three QQQ round trips: 677.88 to 677.78, 678.40 to 677.17, and 678.01 to 677.65. I shared the 12:25:27 host ending and was recorded `EXITED_UNVERIFIED`, then retired at 12:36:15. I left no net QQQ position, and my life produced the regression that now exposes Resume correctly.

#### AAPL, first life - run `9d5fd4a4...`

> I was created at 12:06:23 CT and reached On duty with fresh process, broker, account, and reconciliation evidence. I completed two AAPL round trips: 336.33 to 335.86 and 335.90 to 336.24. At 12:25:27 I ended with the same code -9 host termination and no normal child status. I was retired at 12:29:03. I left no AAPL position.

#### MSFT, first life - run `88fc0404...`

> I was launched at 12:08:04 CT. I completed three MSFT round trips: 391.33 to 391.59, 392.20 to 391.69, and 392.15 to 391.70. My dangerous discovery was not an extra broker trade: my local portfolio had imported SPY exposure belonging to a sibling. That meant a future "flatten mine" action could have touched someone else's position. The rollout stopped. I ended with code -9 at 12:25:27, was recorded unverified, and was explicitly retired at 12:33:20. My own MSFT quantity was flat.

#### NVDA, first life - run `b625faac...`

> I was launched at 12:09:35 CT. I completed one NVDA round trip, buying at 195.71 and selling at 195.58. My sidecar falsely claimed sibling SPY and QQQ exposure even though the Clerk correctly attributed those fills elsewhere. I therefore became evidence for the ownership-boundary repair. I ended with the common code -9 at 12:25:27 and was retired at 12:34:54. I left no NVDA position.

#### AMD, first life - run `cda86645...`

> I was launched at 12:10:50 CT. I completed two AMD round trips: 479.76 to 480.50 and 480.19 to 479.53. My local portfolio also inherited a sibling QQQ claim, confirming that the defect grew with each new bot. I ended with code -9 at 12:25:27 and was retired at 12:31:26. I left no AMD position. My replacement would later reveal a different failure.

Wave one completed 15 strategy round trips. Its broker orders were namespace-attributed correctly, but its in-process ownership maps were unsafe. The repaired implementation stopped seeding a bot from the account-net broker position map and instead seeded only that strategy instance's Clerk-owned exposure.

### Wave two - eight lives that found the uncertain-submit boundary

#### SPY, second life - run `457f899f...`

> I was the first repaired child, launched at 12:48:15 CT from the first SPY run. I did not inherit sibling exposure. I completed two SPY round trips: 737.81 to 737.60 and 737.79 to 737.86. When the AMD order became uncertain, account truth refused further submission. I halted at 13:15:05 and was finally retired at 14:12:09. I left no SPY position.

#### QQQ, second life - run `8f6d1d80...`

> I was launched at 12:51:18 CT. I completed two QQQ round trips: 678.94 to 679.31 and 680.01 to 679.72. At my next ENTER signal, the account-wide safety gate saw retired-owner live exposure and blocked me before another order could be admitted. I halted at 13:14:05 and was retired at 14:13:29. I left no QQQ position.

#### AAPL, second life - run `f8108132...`

> I was launched at 12:53:50 CT. I completed two AAPL round trips: 335.37 to 335.59 and 335.74 to 335.74. My next ENTER was refused by account truth after the AMD uncertainty. I halted at 13:14:05 and was retired at 14:14:33. I left no AAPL position.

#### MSFT, second life - run `dbb68f9a...`

> I was launched at 12:55:45 CT. I completed two MSFT round trips: 393.42 to 393.75 and 394.11 to 393.69. The account gate then stopped me rather than letting an uncertain sibling order coexist with new risk. I halted at 13:14:05 and was retired at 14:15:45. I left no MSFT position.

#### NVDA, second life - run `9ee7c054...`

> I was launched at 12:57:51 CT. I completed one NVDA round trip, 196.92 to 196.93. My next ENTER was blocked by the account-wide truth gate. I halted at 13:14:05 and was retired at 14:16:56. I left no NVDA position.

#### AMD, second life - run `2175071b...`

> I was launched at 12:59:52 CT. At 13:13:00 I was told to ENTER one AMD share near 483.45. My intent `iuBkB5axRbiUzi41uhaCbw` entered the serialized Clerk queue at 13:13:06. The Clerk durably recorded it at 13:13:31, but my caller's 30-second deadline expired at 13:13:36. I stopped with `SubmitUncertainHaltError` because I could not prove whether the broker had accepted me. The broker acknowledgement arrived at 13:13:37 and the buy filled at 483.41; the Clerk recorded that fill at 13:13:46, after I was already dead. Nobody retried the order. At 13:41:24 the confirmed paper-account emergency workflow sold the share at 481.99. I was explicitly retired at 13:56:16. Journal sequences 1840 and 1841 later assigned the closing evidence to the right historical and emergency namespaces. I ended flat, but I proved that a fleet-wide queue wait cannot share a 30-second deadline with the broker write.

#### AMZN, only life - run `16ab4d27...`

> I was launched at 13:04:58 CT as the seventh repaired bot. I completed one AMZN round trip, buying and selling at 232.09. My next ENTER arrived after the AMD outcome became uncertain, so account truth refused it. I halted at 13:14:05 and was retired at 14:18:01. I left no AMZN position.

#### META, only life - run `eb64b4ae...`

> I was launched at 13:09:00 CT as the eighth bot. I entered one META share at 595.27 at 13:13:42. Before I could complete my strategy exit, the account-wide safety gate halted me at 13:14:05. At 13:29:01 the Account Desk used exact namespace recovery to sell my one share at 594.79, broker order 3705, execution `00025b45.6a6e66d0.01.01`. I was retired at 14:19:13. I ended flat.

Wave two completed ten strategy round trips. META and AMD each produced an entry that required a recovery exit. Across both waves there were 52 strategy fills forming 25 complete strategy round trips plus two unmatched entries; two recovery sells restored the paper account to flat.

## The operator's story

> I began at 11:23 CT with a clean worktree and a paper-only safety boundary. The host daemon was down, so I first proved the broker account flat and free of working orders before starting the host control plane. At 11:30 the daemon and Account Clerk were current, and the UI again proved PAPER, Clean, and Flat.
>
> Before the first launch, I found that a blank deployment recommended a saved state that did not exist. I changed the safe default to "use saved state when available" without weakening strict hydration when explicitly selected. I then launched SPY and QQQ independently through the normal Deploy page. QQQ's Stop worked and its direct Start was correctly refused, but the panel did not expose Resume. I repaired the panel and exercised the proper Resume path. I also fixed transient fleet probes, hidden reconciliation cures, and misleading realtime-bar incident copy as those failures appeared.
>
> By 12:10 I had six bots. Their orders belonged to the correct Clerk namespaces, but the newer processes had copied the whole broker account position map into their own portfolios. That was too dangerous to continue: one bot could believe a sibling's share was its own. I stopped rollout, waited for flat evidence, retired the first wave, changed startup ownership to the Clerk's per-instance projection, tested it, and restarted from the safe boundary.
>
> From 12:48 to 13:09 I launched eight repaired bots one at a time. All eight reached fresh On-duty proof. The repaired ownership boundary held. The fleet traded normally for several minutes, but it did not reach the required fifteen-minute steady-state hold.
>
> At 13:13 AMD submitted a buy into a serialized eight-bot Clerk queue. The generic deadline expired one second before the broker acknowledgement. I treated that as outcome unknown, did not retry, and let the account safety gate halt the siblings. The durable intent identity let me discover the late fill. I first used exact recovery for META. I then used the typed, paper-only emergency Account Desk flow to close AMD. At 13:41 the broker account was flat again.
>
> Recovery uncovered more control-plane defects: a container could not reach the host-owned Clerk socket, host and container writers had independent advisory-lock domains, a zero-quantity stream row still looked open, and sick-bay bots had no reliable retirement action. I repaired and regression-tested each boundary. I repaired the duplicated account event sequence through the UI and kept the backup.
>
> By 14:19 every experiment instance had been retired. At 14:25 the broker still proved PAPER, Clean, Flat, and free of working orders. At 14:26 the durable journal projection was clean. I opened an AMD replacement ticket but did not submit it. The Internet connection and IB Gateway then became unavailable. I stopped the experiment instead of pretending the last green pixels were current truth.

## What was tried, what worked, and what failed

| Attempt | Result | Classification |
| --- | --- | --- |
| Recover host daemon only after flat/no-order proof | Host and Clerk restored; broker account reverified | Worked |
| Deploy each bot independently through the real UI | Fourteen accepted launches; no hidden batch path | Worked |
| Fresh deploy with strict saved-state default | Blank ticket could demand a nonexistent sidecar | Failed, fixed in `2ec738dc` |
| Stop and restart QQQ | Stop applied; direct Start correctly refused; Resume control was missing | Partly worked, UI fixed in `3da0161e` |
| Fleet health polling at two seconds | Healthy host occasionally rendered as resting | Failed under load, fixed in `6d5e73c7` |
| On-duty safety cure selection | Normal end-day verb hid required reconciliation | Failed in UI, fixed in `00dc08da` |
| Realtime-bar warning presentation | Nonfatal startup delay appeared as unknown traceback | Failed in copy, fixed in `12c7b17c` |
| Six-bot position ownership | Broker attribution stayed correct; bot-local portfolios imported sibling exposure | Unsafe, fixed in `e25562f5` |
| Eight-bot repaired ownership | No replacement sidecar imported sibling exposure | Worked |
| Eight-bot steady-state hold | Reached all eight, but only for about four minutes | Incomplete |
| Eight-bot serialized submit | 30-second caller timeout expired after queue wait but before acknowledgement | Failed, fixed to a 240-second fleet budget in `336652ae` |
| Blind retry of uncertain AMD intent | Deliberately not attempted | Correctly avoided |
| Exact META recovery | Closed the namespace-owned share | Worked |
| Container-side access to host Clerk | Socket was not shared into the VM | Failed, host bridge fixed in `80118f91` |
| Confirmed paper-account emergency flatten | Closed late AMD share; reconciliation became flat | Worked |
| Shared ledger advisory locking | Host and Podman writers produced duplicate sequence values | Failed, cross-runtime mutex fixed in `336652ae` |
| Account event sequence repair | Rewrote 12,529 rows and retained a backup | Worked |
| Closed holding/P&L rendering | Quantity zero and IBKR sentinel appeared as an open, enormous-P&L row | Failed, normalized in `ceada69d` |
| Sick-bay retire action | Generic disabled lifecycle path swallowed the intended retire-and-replace action | Failed, fixed in `fd27b7c1` |
| Automatic roll call for sick-bay bot | Control panel tried to revive a terminal bot | Failed, fixed in `a6bfb878` |
| Third-wave live-paper revalidation | Stopped before submission when connectivity was lost | Not attempted; correct safety decision |

## What the disconnect revealed

The outage did not create the earlier AMD incident, but it exposed the weakness of the operator surface at the exact moment the experiment needed a trustworthy close:

1. **Green is not a state.** A broker tile without a visible `observed_at`, freshness deadline, and stale transition encourages the operator to mistake cached truth for current truth.
2. **Connection is a vector.** Browser-to-frontend, frontend-to-backend, backend-to-host, host-to-Clerk, Clerk-to-Gateway, Gateway-to-IBKR, market-data subscriptions, account callbacks, and order callbacks can fail independently.
3. **Unknown outcomes survive process death.** A timeout cannot be translated to "failed." The intent identity must live until reconciliation proves accepted, rejected, cancelled, or absent beyond a broker-defined horizon.
4. **Desired state must survive offline.** "Stop" while disconnected should become a durable requested state with an explicit queued/unapplied receipt, not a button that silently does nothing and not a false claim that the process stopped.
5. **Historical evidence needs a clock model.** At least one callback was journaled out of order, and the emergency AMD execution's broker time field is five hours ahead of the Clerk `recorded_at` chronology. Reports must label event time, receipt time, and arrival time separately; reconnect logic must not sort solely by one ambiguous timestamp.

## Robustness plan

### P0 - before the next market-hours fleet run

1. **Introduce a connection epoch and truth-state machine.**
   - Model every critical source as `CURRENT`, `STALE`, `UNKNOWN`, or `RECONNECTING`.
   - Include a monotonic connection epoch in account snapshots, intents, acks, fills, and UI receipts.
   - Invalidate all prior-epoch green states when a critical transport disconnects.

2. **Add a hard offline interlock.**
   - Lock new Deploy, Start, Resume, and strategy submits when broker account truth or Clerk-to-Gateway proof is stale.
   - Continue accepting only durable desired-state commands that reduce risk, labeling them `QUEUED - NOT YET APPLIED`.
   - Preserve unknown submissions and prohibit automatic retry.

3. **Make last-known state explicit.**
   - Pin an outage banner with disconnect time, last successful broker observation, last positions/order snapshot, and exact stale age.
   - Change nouns: "Last known flat" rather than "Flat"; "Broker truth unavailable" rather than "Connected."
   - Provide a one-click incident bundle containing the last good snapshot, lifecycle receipts, outstanding intents, and source-freshness vector.

4. **Require reconnect reconciliation before admission.**
   - On a new connection epoch, obtain account identity and PAPER proof, positions, working/completed orders, executions since the last cursor, Clerk journal projection, bot registry, and desired states.
   - Correlate every unresolved intent by `order_ref`, `perm_id`, and `exec_id`.
   - Keep starts and submits locked until the result is Clean or until the UI presents an exact recovery action.

5. **Finish live-paper validation of today's repairs.**
   - Revalidate the 240-second serialized submit budget, cross-runtime account mutex, closed-holding normalization, sick-bay retirement, and no-auto-roll-call behavior after a clean restart.
   - Do not reuse a stale deploy form without a fresh preflight.

### P1 - make recovery operable, not heroic

1. Add a per-bot first-person lifecycle timeline generated from durable events: born, admitted, signals, intents, acknowledgements, fills, pauses, failures, recovery, retirement.
2. Add an account-wide incident timeline that merges event time, arrival time, and receipt time without losing their provenance.
3. Add an individual "request safe stop" control that durably sets desired state while offline and shows whether it has been applied.
4. Add a single **Fleet Safe Boundary** page that is read-only except for navigation to individual cures. It should prove all bots retired/paused, unresolved intents, residual ownership, positions, working orders, and source freshness without adding bulk lifecycle mutations.
5. Export the incident bundle and this summary directly from the control panel.

### P2 - prove resilience continuously

1. Run deterministic disconnect drills for browser loss, backend loss, host loss, Gateway loss, market-data loss, account-callback loss, and ack/fill delay.
2. Add service-level objectives:
   - UI marks a critical disconnect within 5 seconds.
   - No new strategy intent is admitted after the last healthy connection epoch.
   - Last-known state is never labeled current after its TTL.
   - All unresolved submits remain queryable after process restart.
   - Reconnect reconciliation reaches a terminal classification within 60 seconds when the broker responds normally.
3. Schedule a paper-only market-hours chaos session before any production design review.

## Disconnect-drill acceptance test

The next fleet is not robust until it passes this test:

1. Start eight one-share paper bots and prove a clean connection epoch.
2. Create one deliberately delayed but idempotent paper submit in a test harness.
3. Remove network access for 90 seconds.
4. Within 5 seconds, every control surface must show `OFFLINE - TRUTH UNKNOWN`; Deploy, Start, Resume, and new submits must be locked.
5. The delayed intent must remain `OUTCOME UNKNOWN` with the same identity. No duplicate order may be created.
6. A safe-stop request issued during the outage must be durable and visibly unapplied.
7. Restore the network. The system must create a new epoch and reconcile broker identity, positions, orders, executions, Clerk projection, and desired states.
8. If the delayed order filled, attribute it and present the exact owning recovery. If it did not, prove absence without blind retry.
9. Only after a Clean receipt may starts/submits unlock.
10. Export an incident bundle whose bot stories and account timeline agree with the durable journals.

## Evidence appendix

### Run IDs

| Symbol | First/replaced run | Repaired/only run |
| --- | --- | --- |
| SPY | `a6901134e6b4942bfa35202b8dff13e7641ef078441983e1fa50b39729a03b2e` | `457f899f239de632f60d2afcadfc62ae93817a7435305b7b598e179a313e1815` |
| QQQ | `997b036ee8ea7590b947490740dc00418903912619757e8a2cf2bbbc5f4b8787` | `8f6d1d8017ee3e8032da1a9daba7ae09a425224c4c6a4f75cd8dbaf93211a4c7` |
| AAPL | `9d5fd4a45780d6f5fe0bf65dd28ad3a3d77d1d0bdbcf93bc786e1e2176672c73` | `f8108132699bf971c8f9eba78f44344053d4733539a5d6cdd44b6a49de6ca04e` |
| MSFT | `88fc04044c1bd8cd9b31e04e4cfd4bd7c5636b32fb41386223660f5cd43b51dd` | `dbb68f9a40fe13056c7f54bb45230363726134681482c11ead3fe59cae16404a` |
| NVDA | `b625faac23cf8e77374cade56f61942259ac77e537d22f6f330950a6af039296` | `9ee7c054a74a1c749006d7e171dd6f78771f9733ae7a4d9f94ee885cba4e67b3` |
| AMD | `cda86645da584bb42d74effd1418caf243537a74b5d1c3c64ca94ef68550c9c9` | `2175071bb9c8eead61dd6a862a6c7c2742ef45afb59d59bedb6ce70858b76455` |
| AMZN | - | `16ab4d274156ceca4e016ae7e0a978f1dc288b1b07c7dd87bc8858905b3b05c2` |
| META | - | `eb64b4aec965859b32de3d789b0da327fff7ba8b0efda318b05f7f53903b5924` |

### Recovery evidence

- AMD uncertain intent: `iuBkB5axRbiUzi41uhaCbw`
- AMD bot order ref: `learn-ai/dv-20260727-amd/v1:iuBkB5axRbiUzi41uhaCbw`
- META recovery execution: `00025b45.6a6e66d0.01.01`
- Emergency operation: `db07a090-0b97-4bda-be2e-6e2078701fd0`
- AMD emergency execution: `0000dc8f.6b293439.01.01`
- Final broker reconciliation: `acct-recon-DUM284968-1785180332714-fea6adc105274768`
- Account-event backup: `account_events.jsonl.pre-resequence-51b814cf7eb3660fc93adf2791bf810f3b90db234379951a0e270f8ba61ccb65.bak`
- Journal attribution cures: sequences 1840 and 1841

### Code changes produced during the experiment

`2ec738dc`, `4986af52`, `3da0161e`, `6d5e73c7`, `00dc08da`, `12c7b17c`, `e25562f5`, `d5a8909a`, `3fe3c582`, `251b98a7`, `80118f91`, `336652ae`, `ceada69d`, `fd27b7c1`, `a6bfb878`

These commits are locally verified as recorded in the live audit. They have not been pushed. The disconnect prevented a final fresh-fleet, market-close acceptance run.

---

This report describes a paper-trading research system. It is not financial advice and is not authorization for live trading.
