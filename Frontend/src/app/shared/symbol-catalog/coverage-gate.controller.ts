import { computed, inject, signal } from '@angular/core';

import type { PriceAdjustmentMode } from '../data-lake';
import {
  EnsureCoverageService,
  toBackfillableMode,
  type BackfillableMode,
  type CoverageGateSession,
} from './ensure-coverage.service';
import { formatReceiptLabel } from '../pipes/receipt-label.pipe';

/** What a card needs to hand the gate to decide one pick. */
export interface GateAdmission {
  readonly symbol: string;
  /** The lake tree the host's run reads — the gate's identity (ADR 0066). */
  readonly mode: PriceAdjustmentMode;
  /**
   * Why the lake's coverage verdict is unknowable right now, or `null` when
   * the lake answered. Re-read on every retry, so a recovered lake retries
   * into a real gate instead of replaying its own refusal.
   */
  readonly lakeDark: () => string | null;
  /** Runs when the lake confirms the bars landed. */
  readonly commit: (symbol: string) => void;
}

/**
 * One picker card's ownership of a coverage gate — the state machine the
 * single-symbol card and the multi-symbol card share, extracted so the
 * admission policy and disarm guarantees live once instead of twice.
 *
 * The card constructs this in its field initializers with the adjustment
 * mode the host's run reads (checked again when a backfill finishes, so a
 * gate that outlives its tree is released, not committed). Every exit path
 * — supersede, dismiss, destroy, mode change — funnels through
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

  private pending: GateAdmission | null = null;

  constructor(
    private readonly mode: () => PriceAdjustmentMode | null,
  ) {}

  /** Release the pending gate, if any; safe to call on every teardown path. */
  abandon(): void {
    const session = this.pendingSession();
    if (session === null) return;
    this.pendingSession.set(null);
    this.pending = null;
    void session.cancel();
  }

  /**
   * Decide one pick: refuse while the lake's coverage verdict is unknowable
   * (no gate may run on a guess), refuse a tree nothing can backfill, and
   * otherwise gate the symbol into the tree, committing only when this
   * session is still the pending pick, the lake confirmed coverage, and the
   * host still reads the tree the gate filled.
   */
  admit(admission: GateAdmission): void {
    const { symbol, mode, commit } = admission;
    this.pending = admission;

    const dark = admission.lakeDark();
    if (dark !== null) {
      this.holdRefusal(symbol, mode, 'coverage_unknown', dark, commit);
      return;
    }
    const backfillable = toBackfillableMode(mode);
    if (backfillable === null) {
      // No commit is retained: the strip suppresses Retry for this reason —
      // retrying cannot derive the view; the operator must switch trees.
      this.holdRefusal(
        symbol,
        mode,
        'view_not_backfillable',
        `Nothing derives the ${formatReceiptLabel(mode)} view, so this symbol cannot be backfilled into it. Switch the picker to Raw or Polygon Split Adjusted.`,
        null,
      );
      return;
    }
    this.start(symbol, backfillable, commit);
  }

  /** The strip's Retry: re-decide the pending pick against the current lake. */
  retry(): void {
    const pending = this.pending;
    if (pending === null) return;
    const mode = this.mode();
    if (mode === null) return;
    this.admit({ ...pending, mode });
  }

  private start(symbol: string, mode: BackfillableMode, commit: (symbol: string) => void): void {
    this.abandon();
    this.pending = { symbol, mode, lakeDark: () => null, commit };
    const session = this.coverage.ensure(symbol, mode);
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
      this.pending = null;
      commit(symbol);
    });
  }

  private holdRefusal(
    symbol: string,
    mode: PriceAdjustmentMode,
    reason: string,
    message: string,
    retryCommit: ((symbol: string) => void) | null,
  ): void {
    const session = this.pendingSession();
    if (session !== null) void session.cancel();
    // A refusal with a retry commit keeps it: Retry re-runs `admit` with a
    // fresh lake verdict rather than dead-ending on the strip.
    if (retryCommit === null) this.pending = null;
    this.pendingSession.set(this.coverage.refuse(symbol, mode, reason, message));
  }
}
