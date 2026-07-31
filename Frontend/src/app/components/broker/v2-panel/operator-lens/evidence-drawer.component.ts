import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import type { EvidenceEntry, EvidencePage } from '../lib/broker-v2-panel.types';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';

/**
 * Evidence drawer (spec §14).
 *
 * Operator-gated, bounded/paged, redacted raw evidence for one transaction_ref.
 * Renders inside a slide-out panel; every page load produces a server-side
 * audit entry (the service layer ensures this).
 *
 * Only renders when `open` is true and `transactionRef` is non-null.
 */
@Component({
  selector: 'app-evidence-drawer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent],
  templateUrl: './evidence-drawer.component.html',
  styleUrl: './evidence-drawer.component.scss',
})
export class EvidenceDrawerComponent {
  readonly broker = input.required<string>();
  readonly accountId = input.required<string>();
  readonly sid = input.required<string>();
  readonly transactionRef = input<string | null>(null);
  readonly open = input(false);

  readonly closed = output();

  private readonly panelSvc = inject(BrokerV2PanelService);

  protected readonly page = signal<EvidencePage | null>(null);
  protected readonly loading = signal(false);
  protected readonly loadError = signal<string | null>(null);
  protected readonly cursor = signal<number | undefined>(undefined);

  protected readonly entries = computed(() => this.page()?.entries ?? []);
  protected readonly hasNextPage = computed(() => (this.page()?.next_cursor ?? null) !== null);
  protected readonly totalEntries = computed(() => this.page()?.total_entries ?? 0);
  protected readonly readBy = computed(() => this.page()?.read_by ?? null);
  protected readonly readAt = computed(() => this.page()?.read_at_ms ?? null);

  constructor() {
    // Reload when the drawer opens or the transactionRef changes.
    effect(() => {
      const isOpen = this.open();
      const ref = this.transactionRef();
      if (isOpen && ref) {
        this.cursor.set(undefined);
        void this.loadPage(undefined);
      } else if (!isOpen) {
        this.page.set(null);
        this.loadError.set(null);
      }
    });
  }

  protected async onNextPage(): Promise<void> {
    const next = this.page()?.next_cursor;
    if (next === null || next === undefined) return;
    this.cursor.set(next);
    await this.loadPage(next);
  }

  protected onClose(): void {
    this.closed.emit();
  }

  protected trackEntry(_index: number, entry: EvidenceEntry): number {
    return entry.seq;
  }

  private async loadPage(cursor: number | undefined): Promise<void> {
    const ref = this.transactionRef();
    if (!ref) return;
    this.loading.set(true);
    this.loadError.set(null);
    try {
      const result = await this.panelSvc.getEvidence(
        this.broker(),
        this.accountId(),
        this.sid(),
        {
          transactionRef: ref,
          cursor,
          clientHint: 'operator-lens-drawer',
        },
      );
      this.page.set(result);
    } catch (err) {
      this.loadError.set(
        err instanceof Error ? err.message : 'Failed to load evidence.',
      );
    } finally {
      this.loading.set(false);
    }
  }
}
