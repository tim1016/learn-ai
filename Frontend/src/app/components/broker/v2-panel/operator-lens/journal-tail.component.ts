import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
  signal,
} from '@angular/core';
import type { EvidenceEntry, EvidencePage } from '../lib/broker-v2-panel.types';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { fmtTimestampLocal } from '../../format';

/** The set of allowed kind filter tokens (empty = all). */
const ALL_KINDS = '' as const;

/**
 * Journal tail (spec §7.4).
 *
 * Newest-first order-journal entries from the evidence endpoint.
 * Filterable by kind; each row expands to its summarized receipt and selects
 * its transaction on the rail via `transactionSelected`.
 *
 * The list reveals a small batch at a time so the collapsed audit section
 * remains useful without becoming an unbounded event wall.
 */
@Component({
  selector: 'app-journal-tail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent],
  templateUrl: './journal-tail.component.html',
  styleUrl: './journal-tail.component.scss',
})
export class JournalTailComponent {
  readonly evidencePage = input<EvidencePage | null>(null);
  readonly loading = input(false);

  /** Emits when user selects a row — carries the order_ref to select on the rail. */
  readonly transactionSelected = output<string>();

  protected readonly kindFilter = signal<string>(ALL_KINDS);
  protected readonly visibleCount = signal(5);

  protected readonly filteredEntries = computed(() => {
    const page = this.evidencePage();
    if (!page) return [];
    const filter = this.kindFilter();
    if (!filter) return [...page.entries];
    return page.entries.filter((e) => e.kind === filter);
  });

  protected readonly entries = computed(() =>
    this.filteredEntries().slice(0, this.visibleCount()),
  );

  /**
   * Unique kind values present in the current page, as {kind, label} pairs.
   * Pills display the backend-authored `kind_label` (human copy, §13); the
   * filter key remains the raw `kind` enum string so filtering is exact.
   */
  protected readonly availableKinds = computed(() => {
    const page = this.evidencePage();
    if (!page) return [];
    const seen = new Map<string, string>();
    for (const e of page.entries) {
      if (!seen.has(e.kind)) seen.set(e.kind, e.kind_label);
    }
    return [...seen.entries()].map(([kind, label]) => ({ kind, label }));
  });

  protected readonly hasMoreVisible = computed(() =>
    this.entries().length < this.filteredEntries().length,
  );

  protected readonly hasMoreRecorded = computed(() => {
    const page = this.evidencePage();
    return page !== null && !this.hasMoreVisible() && page.next_cursor !== null;
  });

  protected onSelectRow(entry: EvidenceEntry): void {
    if (entry.order_ref) {
      this.transactionSelected.emit(entry.order_ref);
    }
  }

  protected onKindFilter(kind: string): void {
    this.kindFilter.set(kind);
    this.visibleCount.set(5);
  }

  protected loadMore(): void {
    this.visibleCount.update((count) => count + 5);
  }

  protected entryAriaLabel(entry: EvidenceEntry): string {
    return `${entry.kind_label} at ${fmtTimestampLocal(entry.recorded_at_ms)}`;
  }

  protected trackEntry(_index: number, entry: EvidenceEntry): number {
    return entry.seq;
  }
}
