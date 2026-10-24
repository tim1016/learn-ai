import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { goldenStateLabel } from '../../services/golden-validation.service';
import type { GoldenValidation } from '../../services/golden-validation.types';
import { AssetIdentityComponent } from '../../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { combineReads, type ReadState } from '../read-state';
import { QUALIFICATION_TONES, parameterPairs } from '../signal-strategy-evidence';
import { SignalStrategyChipComponent } from '../signal-strategy-chip.component';
import { SignalStrategySectionComponent } from '../signal-strategy-section.component';
import type { QualifiedVersions } from '../signal-strategy-workspace.store';

/**
 * The configuration values a person or Golden Search approved, one symbol at a
 * time. These are the fourth kind of value: not a default and not the corpus
 * point, but the exact values that carry a review or a qualified version, and
 * each is shown with the status that says whether it still applies.
 */
@Component({
  selector: 'app-signal-strategy-approved-configurations',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent, ReceiptLabelPipe, SignalStrategyChipComponent, SignalStrategySectionComponent],
  templateUrl: './approved-configurations.component.html',
  styleUrl: '../section-content.scss',
})
export class SignalStrategyApprovedConfigurationsComponent {
  readonly versions = input.required<ReadState<QualifiedVersions>>();
  readonly reviews = input.required<ReadState<readonly GoldenValidation[]>>();
  readonly retry = output();

  protected readonly tones = QUALIFICATION_TONES;

  protected readonly read = computed(() => combineReads(this.versions(), this.reviews()));

  protected readonly qualified = computed(() => {
    const state = this.versions();
    if (state.kind !== 'ready') return [];
    return state.value.bySymbol.map((group) => ({
      symbol: group.symbol,
      versions: group.versions.map((version) => ({
        id: version.id,
        status: version.status,
        parameters: parameterPairs(version.parameters),
      })),
    }));
  });

  protected readonly reviewed = computed(() => {
    const state = this.reviews();
    if (state.kind !== 'ready') return [];
    return state.value.map((review) => ({
      id: review.id,
      symbol: review.symbol,
      review: goldenStateLabel(review.state, review.review_is_current),
      parameters: parameterPairs(review.validation_case.parameters),
    }));
  });
}
