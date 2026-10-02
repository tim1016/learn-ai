import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  model,
  signal,
} from '@angular/core';

import type { RecentDecisionView, StrategyViewResponse } from '../../lib/broker-v2-panel.types';
import { TimestampDisplayComponent } from '../../../../../shared/timestamp/timestamp-display.component';
import { ReceiptLabelPipe } from '../../../../../shared/pipes/receipt-label.pipe';
import { StrategyChecksComponent } from '../../strategy-view/strategy-checks.component';
import {
  decisionListEntries,
  type DecisionListEntry,
  type DecisionRowView,
} from '../../strategy-view/strategy-view-model';

let nextDecisionsListId = 0;

/**
 * The bot's recent strategy decisions, shown on the bot page for every mode
 * (issue #1729 AC #8, #2563), oldest first.
 *
 * This is the owner-visible record of what the strategy decided, whether or
 * not a decision became a broker order. `authority_kind` renders through the
 * shared `receiptLabel` pipe so a synthetic (Dry Run) decision stays visibly
 * distinguishable from a Paper or Live one rather than being an invisible
 * field on the wire contract.
 *
 * Each decision shows its checks as ✓/✗ chips and expands to the same table
 * the candle popover shows (#2639). Selecting a row selects its candle on the
 * strategy chart, and a selected candle highlights its row. With the strategy
 * view loaded, the newest bar the bot saw before it started sits above a
 * "Bot started" divider.
 */
@Component({
  selector: 'app-recent-decisions-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, StrategyChecksComponent, TimestampDisplayComponent],
  templateUrl: './recent-decisions-list.component.html',
  styleUrl: './recent-decisions-list.component.scss',
})
export class RecentDecisionsListComponent {
  readonly decisions = input<readonly RecentDecisionView[]>([]);
  readonly strategyView = input<StrategyViewResponse | null>(null);
  /** The selected decision bar, by its close; shared with the strategy chart. */
  readonly selectedBarCloseMs = model<number | null>(null);

  protected readonly idPrefix = `recent-decisions-${nextDecisionsListId++}`;
  protected readonly entries = computed(() => decisionListEntries(this.decisions(), this.strategyView()));
  protected readonly hasDecisions = computed(() => this.decisions().length > 0);
  private readonly expanded = signal<ReadonlySet<string>>(new Set());

  protected entryKey(entry: DecisionListEntry): string {
    return entry.kind === 'row' ? entry.row.key : `start:${entry.startedAtMs}`;
  }

  protected isExpanded(row: DecisionRowView): boolean {
    return this.expanded().has(row.key);
  }

  protected isSelected(row: DecisionRowView): boolean {
    return row.barCloseMs !== null && row.barCloseMs === this.selectedBarCloseMs();
  }

  protected toggle(row: DecisionRowView): void {
    if (row.barCloseMs !== null) this.selectedBarCloseMs.set(row.barCloseMs);
    if (row.explanation === null) return;
    this.expanded.update((current) => {
      const next = new Set(current);
      if (!next.delete(row.key)) next.add(row.key);
      return next;
    });
  }
}
