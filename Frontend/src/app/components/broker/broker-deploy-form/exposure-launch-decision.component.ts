import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { CoherenceConfirmationCardFact, ExposureLaunchDecision } from './deploy-coherence';

@Component({
  selector: 'app-exposure-launch-decision',
  imports: [RouterLink, ReceiptLabelPipe],
  templateUrl: './exposure-launch-decision.component.html',
  styleUrl: './exposure-launch-decision.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ExposureLaunchDecisionComponent {
  readonly decision = input.required<ExposureLaunchDecision>();
  readonly facts = input.required<CoherenceConfirmationCardFact[]>();
  readonly busy = input(false);
  readonly startNow = input(false);
  readonly confirmed = input(false);

  readonly deployOnlyRequested = output();
  readonly confirmStartRequested = output();
}
