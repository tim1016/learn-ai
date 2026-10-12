import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type { SummaryState } from '../setup-summary-checker';

/**
 * The setup in plain sentences, exactly as the server wrote them (the page
 * never rebuilds them), notices first. While the setup does not validate it
 * says the setup is not complete; the fields say why.
 */
@Component({
  selector: 'app-options-backtest-summary',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './summary.component.html',
  styleUrls: ['../options-backtest-card.scss', './summary.component.scss'],
})
export class SummaryComponent {
  readonly state = input.required<SummaryState>();
  readonly retry = output();

  protected readonly summary = computed(() => {
    const state = this.state();
    return state.kind === 'ready' ? state.summary : null;
  });
  protected readonly failure = computed(() => {
    const state = this.state();
    return state.kind === 'failed' ? state.message : null;
  });
  protected readonly otherProblems = computed(() => {
    const state = this.state();
    return state.kind === 'invalid' ? state.problems.other : [];
  });
  protected readonly checking = computed(() => this.state().kind === 'checking');
  protected readonly incomplete = computed(() => {
    const kind = this.state().kind;
    return kind === 'idle' || kind === 'invalid';
  });
}
