import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { percentInputValue } from './golden-search-display';
import type { EvidenceCandidate, SelectionPolicy, StudyScope, TradeActivity } from './golden-search.types';

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
  readonly policy = input.required<SelectionPolicy>();
  readonly finalState = input.required<StudyScope['final_state']>();
  readonly activity = input<TradeActivity | null>(null);

  protected readonly ceiling = computed(() => `${percentInputValue(this.policy().max_drawdown_ceiling)}%`);
  /** The development period's floor: frozen by the plan's expected trade frequency, or its fixed floor. */
  protected readonly floor = computed(() => this.activity()?.windows.find((window) => window.key === 'development')?.minimum_trades ?? this.policy().min_trades);
}
