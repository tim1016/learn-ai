import {
  ChangeDetectionStrategy,
  Component,
  effect,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { BrokerService } from '../../../services/broker.service';
import type { LegacyStaleClaimCandidate } from '../../../api/account-reconciliation.types';

@Component({
  selector: 'app-legacy-stale-claim-cure',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './legacy-stale-claim-cure.component.html',
  styleUrl: './legacy-stale-claim-cure.component.scss',
})
export class LegacyStaleClaimCureComponent {
  private readonly broker = inject(BrokerService);

  readonly accountId = input.required<string>();
  readonly retired = output();
  readonly candidates = signal<LegacyStaleClaimCandidate[]>([]);
  readonly loading = signal(false);
  readonly error = signal<unknown>(null);
  readonly retiringClaimId = signal<string | null>(null);

  constructor() {
    effect(() => {
      const accountId = this.accountId();
      void this.loadCandidates(accountId);
    });
  }

  async retry(): Promise<void> {
    await this.loadCandidates(this.accountId());
  }

  async retire(candidate: LegacyStaleClaimCandidate): Promise<void> {
    if (this.retiringClaimId() !== null) return;
    this.retiringClaimId.set(candidate.claim_id);
    this.error.set(null);
    try {
      await this.broker.retireLegacyStaleClaim(this.accountId(), {
        strategy_instance_id: candidate.strategy_instance_id,
        run_id: candidate.run_id,
        symbol: candidate.symbol,
      });
      this.retired.emit();
      await this.loadCandidates(this.accountId());
    } catch (err) {
      this.error.set(err);
    } finally {
      this.retiringClaimId.set(null);
    }
  }

  private async loadCandidates(accountId: string): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const response = await this.broker.legacyStaleClaimCandidates(accountId);
      this.candidates.set(response.candidates);
    } catch (err) {
      this.error.set(err);
      this.candidates.set([]);
    } finally {
      this.loading.set(false);
    }
  }
}
