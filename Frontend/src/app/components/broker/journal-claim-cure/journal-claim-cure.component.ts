import { ChangeDetectionStrategy, Component, inject, input, output, signal } from '@angular/core';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { BrokerService } from '../../../services/broker.service';
import type { JournalCurePreview, JournalCureReceipt } from '../../../api/account-reconciliation.types';

@Component({
  selector: 'app-journal-claim-cure',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe],
  templateUrl: './journal-claim-cure.component.html',
  styleUrl: './journal-claim-cure.component.scss',
})
export class JournalClaimCureComponent {
  private readonly broker = inject(BrokerService);

  readonly accountId = input.required<string>();
  readonly cured = output<JournalCureReceipt>();
  readonly namespace = signal('');
  readonly symbol = signal('');
  readonly quantity = signal('');
  readonly reason = signal('');
  readonly evidenceRef = signal('');
  readonly preview = signal<JournalCurePreview | null>(null);
  readonly receipt = signal<JournalCureReceipt | null>(null);
  readonly cureAttemptKey = signal<string | null>(null);
  readonly loading = signal(false);
  readonly error = signal<unknown>(null);

  update(field: 'namespace' | 'symbol' | 'quantity' | 'reason' | 'evidenceRef', event: Event): void {
    if (!(event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement)) return;
    this[field].set(event.target.value);
  }

  async previewCure(): Promise<void> {
    const namespace = this.namespace().trim();
    const symbol = this.symbol().trim();
    if (!namespace || !symbol || this.loading()) return;
    this.loading.set(true);
    this.error.set(null);
    this.receipt.set(null);
    this.cureAttemptKey.set(null);
    try {
      this.preview.set(await this.broker.previewJournalCure(this.accountId(), namespace, symbol));
    } catch (error) {
      this.preview.set(null);
      this.error.set(error);
    } finally {
      this.loading.set(false);
    }
  }

  async cure(): Promise<void> {
    const preview = this.preview();
    const signedQuantity = Number(this.quantity());
    const reason = this.reason().trim();
    const evidenceRef = this.evidenceRef().trim();
    if (
      this.loading()
      || !preview?.can_cure
      || !Number.isFinite(signedQuantity)
      || signedQuantity === 0
      || !reason
      || !evidenceRef
    ) {
      return;
    }
    this.loading.set(true);
    this.error.set(null);
    const idempotencyKey = this.cureAttemptKey() ?? crypto.randomUUID();
    this.cureAttemptKey.set(idempotencyKey);
    try {
      const receipt = await this.broker.applyJournalCure(this.accountId(), {
        bot_order_namespace: preview.bot_order_namespace,
        symbol: preview.symbol,
        signed_quantity: signedQuantity,
        reason,
        evidence_refs: [evidenceRef],
        request_provenance: 'account-monitor/journal-cure',
        idempotency_key: idempotencyKey,
      });
      this.receipt.set(receipt);
      this.cured.emit(receipt);
      this.preview.set(null);
      this.cureAttemptKey.set(null);
    } catch (error) {
      this.error.set(error);
    } finally {
      this.loading.set(false);
    }
  }
}
