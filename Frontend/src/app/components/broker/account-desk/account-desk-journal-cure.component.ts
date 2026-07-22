import { ChangeDetectionStrategy, Component, effect, inject, signal } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { BrokerService } from '../../../services/broker.service';
import { extractServerMessage, extractServerReasonCode } from '../operation-error';
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
  readonly transportReady = signal(false);
  readonly transportLoading = signal(false);
  readonly transportErrorMessage = signal<string | null>(null);
  readonly transportReasonCode = signal<string | null>(null);

  constructor() {
    effect(() => {
      this.surface.accountId();
      this.requestGeneration += 1;
      this.preview.set(null);
      this.loading.set(false);
      this.errorMessage.set(null);
      this.transportReady.set(false);
      this.transportLoading.set(false);
      this.transportErrorMessage.set(null);
      this.transportReasonCode.set(null);
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
    this.transportReady.set(false);
    this.transportLoading.set(false);
    this.transportErrorMessage.set(null);
    this.transportReasonCode.set(null);
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

  async recheckClerk(): Promise<void> {
    const accountId = this.surface.accountId();
    const preview = this.preview();
    if (accountId === null || preview === null || !preview.can_cure || this.transportLoading()) return;
    const generation = this.requestGeneration;
    this.transportLoading.set(true);
    this.transportErrorMessage.set(null);
    this.transportReasonCode.set(null);
    try {
      const status = await this.broker.accountServiceStatus(accountId);
      if (!this.isCurrent(accountId, generation)) return;
      this.transportReady.set(status.account_id === accountId);
      if (status.account_id !== accountId) {
        this.transportErrorMessage.set('The Clerk attested a different account. Refresh the cure preview.');
      }
    } catch (error) {
      if (!this.isCurrent(accountId, generation)) return;
      this.transportReady.set(false);
      this.transportErrorMessage.set(
        extractServerMessage(error, 'The Clerk transport check failed. Recheck before requesting this cure.'),
      );
      this.transportReasonCode.set(extractServerReasonCode(error));
    } finally {
      if (this.isCurrent(accountId, generation)) this.transportLoading.set(false);
    }
  }

  requestConfirmation(): void {
    const preview = this.preview();
    if (preview === null || !this.transportReady()) return;
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
