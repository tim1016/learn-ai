import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp/timestamp-display.component';
import type { StrategyValidationSummary } from '../../services/strategy-validation.types';
import type { ReadState } from '../read-state';
import { PROOF_TONES, proofSummaryOf } from '../signal-strategy-evidence';
import { SignalStrategyChipComponent } from '../signal-strategy-chip.component';
import { SignalStrategySectionComponent } from '../signal-strategy-section.component';

/**
 * The program's proof and the one flag a person set on it. Both are about the
 * strategy's program and its validated corpus point: neither says anything
 * about a symbol's settings, which the sections below show one symbol at a time.
 */
@Component({
  selector: 'app-signal-strategy-program-proof',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, SignalStrategyChipComponent, SignalStrategySectionComponent, TimestampDisplayComponent],
  templateUrl: './program-proof.component.html',
  styleUrl: '../section-content.scss',
})
export class SignalStrategyProgramProofComponent {
  readonly proof = input.required<ReadState<StrategyValidationSummary | null>>();
  readonly retry = output();

  protected readonly tones = PROOF_TONES;
  protected readonly summaryOf = proofSummaryOf;
}
