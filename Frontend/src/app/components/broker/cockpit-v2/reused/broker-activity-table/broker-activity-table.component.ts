import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  signal,
} from '@angular/core';
import { TagModule } from 'primeng/tag';

import type { BrokerActivityHealth } from '../../../../../api/live-instances.types';
import { fmtCurrency, fmtNumber, fmtTimestampLocal } from '../../../format';
import { OperatorNoticeComponent } from '../../../../operator-notice/operator-notice.component';

import { BrokerActivityRowDetailComponent } from '../broker-activity-row-detail/broker-activity-row-detail.component';
import type { ActivityBrokerEventRow } from '../bot-trade-chart-card/bot-trade-chart-card.types';
import type {
  BrokerActivityRow,
  Verdict,
} from './broker-activity.types';

interface GroupedRows {
  /** Stable group key — ``perm:<id>`` when perm_id is set, else ``exec:<id|seq>``. */
  key: string;
  /** Display label for the group header — perm_id or "Unmatched". */
  label: string;
  rows: BrokerActivityRow[];
}

/**
 * Broker-activity table — the canonical Activity-tab surface per ADR 0014.
 *
 * Render-only. Consumes a list of backend-authored ``BrokerActivityRow``
 * records (supplied by the parent, which owns the SSE / REST stream)
 * and renders them as a CP-Trades-style executed-fills grid:
 *
 * - Rows with ``verdict === 'engine_only_pending'`` are filtered out
 *   here — they belong in ``WorkingPendingOrdersSectionComponent``.
 *   This is a layout choice (which panel a row appears in), not
 *   business logic; the backend authored every row either way.
 * - Rows are grouped by ``perm_id`` to mirror how IBKR Trades clusters
 *   partial fills under their parent order.
 * - Verdict chip colour is a one-to-one lookup from the closed enum;
 *   the frontend NEVER derives the verdict itself.
 * - Clicking a row toggles a drill-down drawer; the drawer is its own
 *   pure component.
 */
@Component({
  selector: 'app-broker-activity-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BrokerActivityRowDetailComponent, OperatorNoticeComponent, TagModule],
  templateUrl: './broker-activity-table.component.html',
  styleUrl: './broker-activity-table.component.scss',
})
export class BrokerActivityTableComponent {
  readonly rows = input.required<BrokerActivityRow[]>();
  readonly eventRows = input<ActivityBrokerEventRow[] | null>(null);
  readonly backfillLoading = input<boolean>(false);
  readonly backfillError = input<string | null>(null);
  readonly sseStatus = input<string>('connecting');
  readonly sseError = input<string | null>(null);
  /** PR 5 — typed broker-activity health from the 4s status poll.
   *  When present, replaces the implicit ``backfillLoading`` spinner with
   *  the server-authored health verdict. Null before the first poll response. */
  readonly activityHealth = input<BrokerActivityHealth | null>(null);

  /** Executed-trade rows (everything except ``engine_only_pending``). */
  readonly executedRows = computed<BrokerActivityRow[]>(() =>
    this.rows().filter((r) => r.verdict !== 'engine_only_pending'),
  );

  /**
   * Rows grouped by ``perm_id`` — CP Trades clusters partial fills under
   * their parent order. Rows without a perm_id (foreign execs, unmatched)
   * get their own single-row group keyed by ``exec_id`` or ``seq``.
   *
   * Display order: newest group first (by max rendered timestamp in the group);
   * within a group, newest fill first.
   */
  readonly groupedRows = computed<GroupedRows[]>(() => {
    const groups = new Map<string, BrokerActivityRow[]>();
    for (const row of this.executedRows()) {
      const key =
        row.perm_id !== null
          ? `perm:${row.perm_id}`
          : `exec:${row.exec_id ?? row.seq}`;
      const bucket = groups.get(key) ?? [];
      bucket.push(row);
      groups.set(key, bucket);
    }
    const result: GroupedRows[] = [];
    for (const [key, rows] of groups) {
      rows.sort((a, b) => b.ts_ms - a.ts_ms);
      const label =
        rows[0].perm_id !== null ? `Order #${rows[0].perm_id}` : 'Unmatched';
      result.push({ key, label, rows });
    }
    result.sort((a, b) => {
      const aMax = Math.max(...a.rows.map((r) => r.ts_ms));
      const bMax = Math.max(...b.rows.map((r) => r.ts_ms));
      return bMax - aMax;
    });
    return result;
  });

  readonly hasRows = computed<boolean>(() => this.executedRows().length > 0);
  readonly hasEventRows = computed<boolean>(() => (this.eventRows()?.length ?? 0) > 0);

  private readonly expanded = signal<Set<number>>(new Set());
  private readonly expandedEvents = signal<Set<string>>(new Set());

  isExpanded(seq: number): boolean {
    return this.expanded().has(seq);
  }

  toggleRow(seq: number): void {
    this.expanded.update((s) => {
      const next = new Set(s);
      if (next.has(seq)) next.delete(seq);
      else next.add(seq);
      return next;
    });
  }

  isEventExpanded(row: ActivityBrokerEventRow): boolean {
    return this.expandedEvents().has(row.visible_row_id);
  }

  toggleEvent(row: ActivityBrokerEventRow): void {
    this.expandedEvents.update((s) => {
      const next = new Set(s);
      if (next.has(row.visible_row_id)) next.delete(row.visible_row_id);
      else next.add(row.visible_row_id);
      return next;
    });
  }

  evidenceIdentity(ref: ActivityBrokerEventRow['evidence'][number]): string {
    const parts = [
      ref.order_ref ? `ref ${ref.order_ref}` : null,
      ref.exec_id ? `exec ${ref.exec_id}` : null,
      ref.perm_id !== null && ref.perm_id !== undefined ? `perm ${ref.perm_id}` : null,
      ref.order_id !== null && ref.order_id !== undefined ? `order ${ref.order_id}` : null,
      ref.symbol ? ref.symbol : null,
    ].filter((part): part is string => part !== null);
    return parts.length > 0 ? parts.join(' · ') : 'No row identity captured';
  }

  /** Closed-enum chip class — frontend picks the colour from the enum;
   * it does NOT derive the enum itself. */
  verdictClass(v: Verdict): string {
    switch (v) {
      case 'expected':
        return 'verdict-expected';
      case 'expected_with_caveat':
        return 'verdict-caveat';
      case 'unexpected':
        return 'verdict-unexpected';
      case 'engine_only_pending':
        return 'verdict-pending';
    }
  }

  verdictLabel(v: Verdict): string {
    switch (v) {
      case 'expected':
        return 'Expected';
      case 'expected_with_caveat':
        return 'Expected (caveat)';
      case 'unexpected':
        return 'Unexpected';
      case 'engine_only_pending':
        return 'Pending';
    }
  }

  /** Render-only formatting wrappers used by the template. */
  readonly fmtCurrency = fmtCurrency;
  readonly fmtNumber = fmtNumber;
  readonly fmtTimestampLocal = fmtTimestampLocal;

  formatLag(ms: number | null | undefined): string {
    if (ms === null || ms === undefined) return '—';
    return `${fmtNumber(ms, 0)} ms`;
  }

  trackGroup = (_i: number, g: GroupedRows): string => g.key;
  trackRow = (_i: number, r: BrokerActivityRow): number => r.seq;
  trackEventRow = (_i: number, r: ActivityBrokerEventRow): string => r.visible_row_id;
  trackEvidence = (_i: number, r: ActivityBrokerEventRow['evidence'][number]): number => r.seq;
}
