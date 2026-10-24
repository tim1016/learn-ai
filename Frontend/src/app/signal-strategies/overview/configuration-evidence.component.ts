import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { GOLDEN_VALIDATION_LIST_LIMIT, goldenStateLabel } from '../../services/golden-validation.service';
import type { GoldenValidation } from '../../services/golden-validation.types';
import { AssetIdentityComponent } from '../../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { ALWAYS_READY, type ReadState } from '../read-state';
import { parameterPairs, type StrategyRegistryEntry } from '../signal-strategy-evidence';
import { SignalStrategySectionComponent } from '../signal-strategy-section.component';

/**
 * Evidence about one symbol and one exact configuration at a time: the corpus
 * point the registry's program was validated at, and the configurations a
 * person reviewed. A review for one symbol is never read as covering another,
 * and neither is a summary of the strategy.
 */
@Component({
  selector: 'app-signal-strategy-configuration-evidence',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent, ReceiptLabelPipe, SignalStrategySectionComponent],
  templateUrl: './configuration-evidence.component.html',
  styleUrl: '../section-content.scss',
})
export class SignalStrategyConfigurationEvidenceComponent {
  readonly entry = input.required<StrategyRegistryEntry>();
  readonly reviews = input.required<ReadState<readonly GoldenValidation[]>>();
  readonly retry = output();

  /** The corpus point is the registry's own declaration, so nothing is read for it. */
  protected readonly declared = ALWAYS_READY;

  protected readonly listLimit = GOLDEN_VALIDATION_LIST_LIMIT;

  protected readonly corpusPoint = computed(() => parameterPairs(this.entry().validated_settings));

  /** The read returns the newest reviews up to a limit; one that long may have left older ones out. */
  protected readonly mayBeCut = computed(() => {
    const state = this.reviews();
    return state.kind === 'ready' && state.value.length >= GOLDEN_VALIDATION_LIST_LIMIT;
  });

  protected readonly rows = computed(() => {
    const state = this.reviews();
    if (state.kind !== 'ready') return [];
    return state.value.map((review) => ({
      id: review.id,
      symbol: review.symbol,
      review: goldenStateLabel(review.state, review.review_is_current),
      classification: review.latest_review?.classification ?? null,
      parameters: parameterPairs(review.validation_case.parameters),
      sourceRunId: review.source_run_id,
    }));
  });
}
