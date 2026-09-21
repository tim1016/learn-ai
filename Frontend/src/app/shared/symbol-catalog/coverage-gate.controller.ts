import { computed, inject, signal } from '@angular/core';

import type { PriceAdjustmentMode } from '../data-lake';
import {
  EnsureCoverageService,
  type BackfillableMode,
  type CoverageGateSession,
} from './ensure-coverage.service';

/**
 * One picker card's ownership of a coverage gate — the state machine the
 * single-symbol card and the multi-symbol card share, extracted so the
 * disarm guarantees live once instead of twice.
 *
 * The card constructs this in its field initializers with two callbacks:
 * the adjustment mode the host's run reads (checked again when a backfill
 * finishes, so a gate that outlives its tree is released, not committed)
 * and the commit to run when the lake confirms the bars landed. Every exit
 * path — supersede, dismiss, destroy, mode change — funnels through
 * {@link abandon}, which releases exactly this controller's session; the
 * coverage service stops the underlying run only when no other card waits
 * on it, so a release here can never cancel a gate another card holds.
 */
export class CoverageGateController {
  private readonly coverage = inject(EnsureCoverageService);

  /**
   * This card's gate, if the operator's pending pick is the gated one. The
   * handle is the ownership — the coverage service is app-scoped, and no
   * field comparison can say which card a running gate belongs to.
   */
  readonly pendingSession = signal<CoverageGateSession | null>(null);
  readonly state = computed(() => this.pendingSession()?.state() ?? null);

  private pendingCommit: ((symbol: string) => void) | null = null;

  constructor(
    /**
     * The tree the host's run reads, re-read when a backfill finishes. A
     * host whose mode can go `null` (the multi card's no-gate mode) makes a
     * pending gate stale the moment it does — `null !== mode` releases it.
     */
    private readonly mode: () => PriceAdjustmentMode | null,
  ) {}

  /** Release the pending gate, if any; safe to call on every teardown path. */
  abandon(): void {
    const session = this.pendingSession();
    if (session === null) return;
    this.pendingSession.set(null);
    this.pendingCommit = null;
    void session.cancel();
  }

  /**
   * Gate `symbol` into `mode`'s tree, committing only when this session is
   * still the pending pick, the lake confirmed coverage, and the host still
   * reads the tree the gate filled.
   */
  start(symbol: string, mode: BackfillableMode, commit: (symbol: string) => void): void {
    this.abandon();
    const session = this.coverage.ensure(symbol, mode);
    this.pendingCommit = commit;
    this.pendingSession.set(session);
    void session.done.then((ready) => {
      // The operator may have picked another instrument — or dismissed the
      // gate — while this backfill ran. Only the gate that is still the
      // pending pick may commit.
      if (this.pendingSession() !== session) return;
      if (!ready) return;
      if (this.mode() !== mode) {
        this.abandon();
        return;
      }
      this.pendingSession.set(null);
      this.pendingCommit = null;
      commit(symbol);
    });
  }

  /** Re-run the pending gate's pick — the strip's Retry. */
  retry(mode: BackfillableMode): void {
    const state = this.state();
    const commit = this.pendingCommit;
    if (state === null || commit === null) return;
    this.start(state.symbol, mode, commit);
  }

  /**
   * Record a refusal the gate cannot act on — an unbackfillable view, say.
   * The strip renders it exactly like a failed run; nothing is committed.
   */
  refuse(
    symbol: string,
    mode: PriceAdjustmentMode,
    reason: string,
    message: string,
  ): void {
    this.abandon();
    this.pendingSession.set(this.coverage.refuse(symbol, mode, reason, message));
  }
}
