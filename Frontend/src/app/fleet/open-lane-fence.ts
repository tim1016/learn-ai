/** The lane-fence-freeze wiring shared by every command surface that must
 * freeze a lane's binding fence at OPEN time and never re-derive it from a
 * live directory read (#2068).
 *
 * `linkedSignal`'s `computation` tracks every signal it reads AND runs
 * lazily — only on the first read after `source` changes. Both properties
 * are exploited here, and both are load-bearing:
 *  - `computation` wraps the caller's `freeze` read in `untracked()`, so a
 *    live directory refresh (the #2068 mitigating
 *    `FleetDirectoryService.refresh()` on a stale-generation refusal, or an
 *    unrelated background poll) does not silently re-derive — and therefore
 *    un-freeze — the fence.
 *  - the eager `effect()` below forces `computation` to actually run as
 *    soon as the lane renders, rather than the first time some click
 *    handler happens to read the returned signal, which would freeze at
 *    CLICK time, not OPEN time, defeating the fence entirely.
 *
 * Dropping either one silently un-freezes the fence — this is exactly the
 * #2068 bug, and this branch's own history includes getting it wrong once.
 * Concentrating both here means every command surface that calls this
 * inherits the same proof rather than re-deriving it byte-for-byte.
 *
 * Deliberately takes a `freeze` callback rather than a `FleetDirectoryService`
 * / `broker` / `clerkId` triple: the caller still calls `freezeLaneFence(`
 * itself, so a file that mints a command from a live directory read stays
 * visible to `lane-fence-freeze.contract.spec.ts`'s textual check — that
 * spec greps each file for the literal call, and a fully opaque helper would
 * have made every caller invisible to it.
 *
 * Must be called from a component field initializer or constructor — an
 * Angular injection context — because it creates the `effect()` below.
 */
import { effect, linkedSignal, untracked, type Signal } from '@angular/core';

import type { LaneFence } from './lane-fence';

export function openLaneFence(freeze: () => LaneFence, source: () => string): Signal<LaneFence> {
  const fence = linkedSignal({
    source,
    computation: (): LaneFence => untracked(freeze),
  });
  // Materialize the fence as soon as the lane renders. linkedSignal is lazy:
  // a value only ever read inside an action handler would first compute at
  // CLICK time, not OPEN time, silently freezing nothing (#2068).
  effect(() => {
    fence();
  });
  return fence;
}
