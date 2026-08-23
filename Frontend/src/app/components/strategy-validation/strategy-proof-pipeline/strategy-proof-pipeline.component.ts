import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { StrategyProofDossier } from '../../../services/strategy-validation.types';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';

@Component({
  selector: 'app-strategy-proof-pipeline',
  imports: [ReceiptLabelPipe],
  templateUrl: './strategy-proof-pipeline.component.html',
  styleUrl: './strategy-proof-pipeline.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyProofPipelineComponent {
  readonly proof = input.required<StrategyProofDossier>();
}
