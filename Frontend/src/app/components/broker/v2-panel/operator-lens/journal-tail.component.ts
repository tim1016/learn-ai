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

/** The set of allowed kind filter tokens (empty = all). */
const ALL_KINDS = '' as const;

/**
 * Journal tail (spec §7.4).
 *
 * Newest-first order-journal entries from the evidence endpoint.
 * Filterable by kind; each row expands to its summarized receipt and selects
 * its transaction on the rail via `transactionSelected`.
 *
 * Evidence raw links are handled by the parent operator lens (evidence drawer).
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
  /** Emits to request opening the evidence drawer for a specific transaction_ref. */
  readonly evidenceDrawerRequested = output<string>();

  protected readonly kindFilter = signal<string>(ALL_KINDS);
  protected readonly expandedSeqs = signal<ReadonlySet<number>>(new Set());

  protected readonly entries = computed(() => {
    const page = this.evidencePage();
    if (!page) return [];
    const filter = this.kindFilter();
    if (!filter) return [...page.entries];
    return page.entries.filter((e) => e.kind === filter);
  });

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

  protected readonly hasMore = computed(() => {
    const page = this.evidencePage();
    return page !== null && page.next_cursor !== null;
  });

  protected onSelectRow(entry: EvidenceEntry): void {
    if (entry.order_ref) {
      this.transactionSelected.emit(entry.order_ref);
    }
  }

  protected onEvidenceDrawer(entry: EvidenceEntry): void {
    const ref = entry.order_ref;
    if (ref) {
      this.evidenceDrawerRequested.emit(ref);
    }
  }

  protected onKindFilter(kind: string): void {
    this.kindFilter.set(kind);
  }

  protected toggleExpand(seq: number): void {
    this.expandedSeqs.update((prev) => {
      const next = new Set(prev);
      if (next.has(seq)) {
        next.delete(seq);
      } else {
        next.add(seq);
      }
      return next;
    });
  }

  protected isExpanded(seq: number): boolean {
    return this.expandedSeqs().has(seq);
  }

  protected trackEntry(_index: number, entry: EvidenceEntry): number {
    return entry.seq;
  }
}
