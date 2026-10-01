import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import { AssetIdentityComponent } from '../../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { EXPOSURE_LABELS } from './golden-search-display';
import type { StrategyCapability, StudyDetail } from './golden-search.types';

/**
 * The study heading and data strip (#2696): strategy and instrument, the
 * frozen protocol, the record's revision and the final test's exposure, plus
 * the state tags. The intervals are on the scope line beneath the steps.
 */
@Component({
  selector: 'app-golden-search-study-strip',
  imports: [AssetIdentityComponent, ReceiptLabelPipe, RouterLink, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-study-strip.component.html',
  styleUrl: './golden-search-study-strip.component.scss',
})
export class GoldenSearchStudyStripComponent {
  readonly study = input.required<StudyDetail>();
  readonly capability = input<StrategyCapability | null>(null);

  /** The final test's exposure as the server recorded it; before it is opened, that it is still held back. */
  protected readonly exposure = computed(() => {
    const study = this.study();
    const exam = study.results.exam;
    if (exam !== null) return `${EXPOSURE_LABELS[exam.exposure_state]} · ${exam.claim}`;
    if (study.exposure_claim !== null) return `Opened · ${study.exposure_claim}`;
    return study.exam_locked ? 'Opened' : 'Held back — not opened by this study';
  });
}
