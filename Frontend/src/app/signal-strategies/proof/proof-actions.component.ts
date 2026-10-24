import { ChangeDetectionStrategy, Component, inject, input, output, signal } from '@angular/core';

import { StrategyValidationService } from '../../services/strategy-validation.service';
import type { StrategyValidationDetail, StrategyValidationFlag } from '../../services/strategy-validation.types';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp/timestamp-display.component';

/**
 * The two things a person does to a strategy's program proof: recheck the stored
 * proof, and set the strategy-level human flag (Accept or Reject, with a required
 * reason, and for a production candidate the QuantConnect backtest ID that
 * acceptance needs). The flag is the program's, recorded once for the strategy; it
 * is not an approval of any symbol or any settings, which only Golden gives.
 * After either action the host reads the proof again.
 */
@Component({
  selector: 'app-proof-actions',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './proof-actions.component.html',
  styleUrl: './proof-actions.component.scss',
})
export class ProofActionsComponent {
  private readonly service = inject(StrategyValidationService);

  readonly detail = input.required<StrategyValidationDetail>();
  /** A recheck or a saved flag has changed what the proof says: read it again. */
  readonly changed = output();

  protected readonly flagChoice = signal<StrategyValidationFlag>('validated');
  protected readonly flagReason = signal('');
  protected readonly backtestId = signal('');
  protected readonly busy = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly message = signal<string | null>(null);

  protected isHarness(): boolean {
    return this.detail().strategy_category === 'operational_validation_harness';
  }

  protected flagLabel(flag: StrategyValidationFlag): string {
    return flag === 'validated' ? 'Accepted' : 'Rejected';
  }

  protected setFlagChoice(event: Event): void {
    if (!(event.target instanceof HTMLInputElement)) return;
    const value = event.target.value;
    if (value === 'validated' || value === 'invalidated') this.flagChoice.set(value);
  }

  protected setFlagReason(event: Event): void {
    if (event.target instanceof HTMLTextAreaElement) this.flagReason.set(event.target.value);
  }

  protected setBacktestId(event: Event): void {
    if (event.target instanceof HTMLInputElement) this.backtestId.set(event.target.value);
  }

  protected async refresh(): Promise<void> {
    if (this.busy()) return;
    this.begin();
    try {
      const result = await this.service.refreshValidationEvidence(this.detail().strategy_key);
      this.message.set(`Stored proof rechecked from ${result.refresh_id}.`);
      this.changed.emit();
    } catch {
      this.error.set('Validation evidence could not be refreshed.');
    } finally {
      this.busy.set(false);
    }
  }

  protected async submit(): Promise<void> {
    const detail = this.detail();
    const reason = this.flagReason().trim();
    if (this.busy()) return;
    if (reason === '') {
      this.error.set('A validation reason is required.');
      return;
    }
    const backtestId = this.backtestId().trim() || detail.qc_cloud_backtest_id?.trim() || '';
    const requiresQuantConnect = detail.strategy_category === 'production_candidate';
    if (this.flagChoice() === 'validated' && requiresQuantConnect && backtestId === '') {
      this.error.set('A QC Cloud backtest ID is required to accept validation evidence.');
      return;
    }
    this.begin();
    try {
      const saved = await this.service.flagValidation(detail.strategy_key, {
        flag: this.flagChoice(),
        reason,
        ...(this.flagChoice() === 'validated' && requiresQuantConnect && backtestId !== '' ? { qc_cloud_backtest_id: backtestId } : {}),
      });
      this.message.set(`${this.flagLabel(saved.current_flag_event?.flag ?? this.flagChoice())} flag saved.`);
      this.flagReason.set('');
      this.changed.emit();
    } catch {
      this.error.set('Validation flag could not be saved.');
    } finally {
      this.busy.set(false);
    }
  }

  private begin(): void {
    this.busy.set(true);
    this.error.set(null);
    this.message.set(null);
  }
}
