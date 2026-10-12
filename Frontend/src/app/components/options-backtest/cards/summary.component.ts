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
  /**
   * The one line a screen reader is told about: it is the status of the
   * summary (being read, read, not complete, not read) and nothing of the
   * summary's own text, which would otherwise be read out again at every
   * pause in the editing.
   */
  protected readonly statusText = computed(() => {
    const state = this.state();
    switch (state.kind) {
      case 'checking':
        return 'Reading the setup back…';
      case 'ready':
        return state.summary.notices.length > 0 ? 'The server has read this setup back. Read its notices first.' : 'The server has read this setup back.';
      case 'failed':
        return state.message;
      case 'invalid':
        // What is missing is at the fields, unless the server's only reasons are about the setup as a whole.
        return state.problems.fields.size > 0
          ? NOT_COMPLETE_AT_FIELDS
          : 'This setup is not complete. The problem is about the setup as a whole; it is listed below.';
      case 'idle':
        return NOT_COMPLETE_AT_FIELDS;
    }
  });
}

const NOT_COMPLETE_AT_FIELDS = 'This setup is not complete. The fields marked above say what is missing.';
