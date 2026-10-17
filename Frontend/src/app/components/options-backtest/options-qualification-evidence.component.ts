import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import type { GoldenOptionSetup } from './golden-option-setups.service';

/** Permanent qualification exceptions accompany the version wherever it is reviewed. */
@Component({
  selector: 'app-options-qualification-evidence',
  imports: [ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-qualification-evidence.component.html',
})
export class OptionsQualificationEvidenceComponent {
  readonly evidence = input.required<GoldenOptionSetup['evidence']>();
}
