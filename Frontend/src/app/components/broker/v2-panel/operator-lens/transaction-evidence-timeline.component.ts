import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  signal,
  untracked,
} from '@angular/core';

import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import type { EvidenceEntry } from '../lib/broker-v2-panel.types';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';

/** On-demand, paged transaction evidence rendered inside a station disclosure. */
@Component({
  selector: 'app-transaction-evidence-timeline',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent],
  templateUrl: './transaction-evidence-timeline.component.html',
  styleUrl: './transaction-evidence-timeline.component.scss',
})
export class TransactionEvidenceTimelineComponent {
  readonly broker = input.required<string>();
  readonly accountId = input.required<string>();
  readonly sid = input.required<string>();
  readonly transactionRef = input.required<string>();

  private readonly panelSvc = inject(BrokerV2PanelService);
  private requestGeneration = 0;

  protected readonly entries = signal<readonly EvidenceEntry[]>([]);
  protected readonly nextCursor = signal<number | null>(null);
  protected readonly totalEntries = signal(0);
  protected readonly loading = signal(false);
  protected readonly loadError = signal<string | null>(null);
  protected readonly hasNextPage = computed(() => this.nextCursor() !== null);

  constructor() {
    effect(() => {
      this.broker();
      this.accountId();
      this.sid();
      this.transactionRef();
      untracked(() => void this.resetAndLoad());
    });
  }

  protected async loadMore(): Promise<void> {
    const cursor = this.nextCursor();
    if (cursor === null || this.loading()) return;
    await this.loadPage(cursor, false, this.requestGeneration);
  }

  protected retry(): void {
    void this.resetAndLoad();
  }

  protected trackEntry(_index: number, entry: EvidenceEntry): number {
    return entry.seq;
  }

  private async resetAndLoad(): Promise<void> {
    const generation = ++this.requestGeneration;
    this.entries.set([]);
    this.nextCursor.set(null);
    this.totalEntries.set(0);
    await this.loadPage(undefined, true, generation);
  }

  private async loadPage(
    cursor: number | undefined,
    replace: boolean,
    generation: number,
  ): Promise<void> {
    this.loading.set(true);
    this.loadError.set(null);
    try {
      const page = await this.panelSvc.getEvidence(
        this.broker(),
        this.accountId(),
        this.sid(),
        {
          transactionRef: this.transactionRef(),
          cursor,
          pageSize: 12,
          clientHint: 'operator-transaction-timeline',
        },
      );
      if (generation !== this.requestGeneration) return;
      this.entries.update((current) => replace ? page.entries : [...current, ...page.entries]);
      this.nextCursor.set(page.next_cursor);
      this.totalEntries.set(page.total_entries);
    } catch (error) {
      if (generation !== this.requestGeneration) return;
      this.loadError.set(error instanceof Error ? error.message : 'Could not load transaction evidence.');
    } finally {
      if (generation === this.requestGeneration) this.loading.set(false);
    }
  }
}
