import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { fmtNumber, fmtTimestampNy } from '../../../format';

import type { BrokerActivityRow } from '../broker-activity-table/broker-activity.types';

interface PendingDisplay {
  row: BrokerActivityRow;
  /** Backend-provided intent timestamp (ts_ms) — frontend formats only. */
  intentTs: string;
  intentId: string | null;
}

/**
 * Working / Pending orders — CP Trades-style panel for rows whose
 * verdict is ``engine_only_pending`` (engine emitted intent, no broker
 * ack yet). Rendered separately from executed trades to mirror the
 * Client Portal layout the operator already knows.
 *
 * Render-only. The filter on ``verdict === 'engine_only_pending'`` is a
 * presentational layout choice, NOT business logic — the backend
 * authored every one of these rows; we just put them in their own panel.
 */
@Component({
  selector: 'app-working-pending-orders-section',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './working-pending-orders-section.component.html',
  styleUrl: './working-pending-orders-section.component.scss',
})
export class WorkingPendingOrdersSectionComponent {
  readonly rows = input.required<BrokerActivityRow[]>();

  readonly pendingRows = computed<PendingDisplay[]>(() => {
    // Slice 7 (handoff gap #1) — when the publisher authors a pending
    // row, the eventual broker fill/cancel row arrives as a separate
    // seq with the same ``order_ref``. Suppress the pending row once
    // any later row supersedes it so the Working/Pending panel reacts
    // to broker activity without operator action.
    const all = this.rows();
    const supersededOrderRefs = new Set<string>();
    for (const r of all) {
      if (r.verdict !== 'engine_only_pending' && r.order_ref) {
        supersededOrderRefs.add(r.order_ref);
      }
    }
    return all
      .filter((r) => r.verdict === 'engine_only_pending')
      .filter((r) => !(r.order_ref && supersededOrderRefs.has(r.order_ref)))
      .map((r) => ({
        row: r,
        intentTs: fmtTimestampNy(r.ts_ms),
        intentId: r.engine_overlay?.intent_id ?? null,
      }))
      .sort((a, b) => b.row.ts_ms - a.row.ts_ms);
  });

  readonly hasPending = computed<boolean>(() => this.pendingRows().length > 0);

  readonly fmtNumber = fmtNumber;

  trackRow = (_i: number, p: PendingDisplay): number => p.row.seq;
}
