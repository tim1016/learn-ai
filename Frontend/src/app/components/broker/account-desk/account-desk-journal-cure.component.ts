import { ChangeDetectionStrategy, Component, effect, inject, signal } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { BrokerService } from '../../../services/broker.service';
import type { JournalCurePreview } from '../../../api/account-reconciliation.types';
import { AccountDeskRecoveryStore } from './account-desk-recovery-store.service';
import { AccountDeskSurfaceStore } from './account-desk-surface-store.service';

/** Preview-first form for a server-constrained Clerk journal adjustment. */
@Component({
  selector: 'app-account-desk-journal-cure',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe],
  templateUrl: './account-desk-journal-cure.component.html',
  styleUrl: './account-desk-journal-cure.component.scss',
})
export class AccountDeskJournalCureComponent {
  private readonly broker = inject(BrokerService);
  private readonly surface = inject(AccountDeskSurfaceStore);
  readonly recovery = inject(AccountDeskRecoveryStore);
  private requestGeneration = 0;

  readonly namespace = signal('');
  readonly symbol = signal('');
  readonly quantity = signal('');
  readonly reason = signal('');
  readonly evidenceRef = signal('');
  readonly preview = signal<JournalCurePreview | null>(null);
  readonly loading = signal(false);
  readonly errorMessage = signal<string | null>(null);

  constructor() {
    effect(() => {
      this.surface.accountId();
      this.requestGeneration += 1;
      this.preview.set(null);
      this.loading.set(false);
      this.errorMessage.set(null);
    });
  }

  update(field: 'namespace' | 'symbol' | 'quantity' | 'reason' | 'evidenceRef', event: Event): void {
    const target = event.target;
    if (!(target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement)) return;
    this[field].set(target.value);
  }

  async previewCure(): Promise<void> {
    const accountId = this.surface.accountId();
    const namespace = this.namespace().trim();
    const symbol = this.symbol().trim();
    if (accountId === null || namespace.length === 0 || symbol.length === 0 || this.loading()) return;
    const generation = ++this.requestGeneration;
    this.loading.set(true);
    this.errorMessage.set(null);
    try {
      const preview = await this.broker.previewJournalCure(accountId, namespace, symbol);
      if (!this.isCurrent(accountId, generation)) return;
      this.preview.set(preview);
    } catch {
      if (this.isCurrent(accountId, generation)) {
        this.preview.set(null);
        this.errorMessage.set('Journal cure preview is unavailable. Review the current account proof and retry.');
      }
    } finally {
      if (this.isCurrent(accountId, generation)) this.loading.set(false);
    }
  }

  requestConfirmation(): void {
    const preview = this.preview();
    if (preview === null) return;
    this.recovery.requestJournalCure(
      preview,
      Number(this.quantity()),
      this.reason(),
      this.evidenceRef(),
    );
  }

  private isCurrent(accountId: string, generation: number): boolean {
    return this.surface.accountId() === accountId && this.requestGeneration === generation;
  }
}
