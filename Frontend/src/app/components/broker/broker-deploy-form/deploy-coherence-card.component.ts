import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { CoherenceConfirmationCardFact } from './deploy-coherence';

@Component({
  selector: 'app-deploy-coherence-card',
  imports: [ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './deploy-coherence-card.component.html',
  styleUrl: './deploy-coherence-card.component.scss',
})
export class DeployCoherenceCardComponent {
  readonly title = input.required<string>();
  readonly ariaLabel = input.required<string>();
  readonly facts = input.required<CoherenceConfirmationCardFact[]>();
  readonly confirmed = input<boolean>(false);
  readonly startNow = input<boolean>(false);
  readonly confirmLabel = input.required<string>();
  readonly confirmedText = input<string>('Confirmed for this Deploy & start.');
  readonly deployOnlyText = input<string>('Deploy-only will stage these values without starting.');

  readonly confirmationRequested = output<boolean>();

  confirm(): void {
    this.confirmationRequested.emit(true);
  }
}
