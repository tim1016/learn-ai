import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import { optionsStudyRoute } from '../../options-setups/options-setups.address';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import type { GoldenOptionSetup } from './golden-option-setups.service';

/** Permanent qualification exceptions accompany the version wherever it is reviewed. */
@Component({
  selector: 'app-options-qualification-evidence',
  imports: [ReceiptLabelPipe, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-qualification-evidence.component.html',
})
export class OptionsQualificationEvidenceComponent {
  readonly evidence = input.required<GoldenOptionSetup['evidence']>();
  protected readonly studyRoute = optionsStudyRoute;
}
