import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { percentInputValue, studyTradeFloor, usesTradeFrequency } from './golden-search-display';
import type { EvidenceCandidate, StudyDetail } from './golden-search.types';

/**
 * What choosing this candidate implies (#2696): the server's guidance for the
 * candidate's situation beside the frozen rules it will be judged by — the
 * loss ceiling, the development period's trade floor, whether the final test is still locked —
 * and the honest note that no luck adjustment can be estimated.
 */
@Component({
  selector: 'app-golden-search-candidate-aside',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-candidate-aside.component.html',
  styleUrl: './golden-search-candidate-aside.component.scss',
})
export class GoldenSearchCandidateAsideComponent {
  readonly candidate = input.required<EvidenceCandidate>();
  readonly study = input.required<StudyDetail>();

  protected readonly ceiling = computed(() => `${percentInputValue(this.study().protocol.policy.max_drawdown_ceiling)}%`);
  protected readonly frequencyBased = computed(() => usesTradeFrequency(this.study().protocol));
  protected readonly floor = computed(() => studyTradeFloor(this.study(), 'development'));
}
