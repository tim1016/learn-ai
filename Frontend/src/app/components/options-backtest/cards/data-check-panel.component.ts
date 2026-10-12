import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { CheckState } from '../data-check-flow';
import type { DataCheck, TestableStretch } from '../options-backtest.types';
import { DataCheckSessionsComponent } from './data-check-sessions.component';

/**
 * What Check data found, kept open until the owner closes it: how many
 * sessions are ready, need a download, or cannot be tested, and why. When not
 * every session can be tested it offers Run available dates, a button that
 * writes the longest testable stretch into the dates; the dates are never
 * shortened without that click. An answer is for the setup and dates it was
 * asked with, and says so when either has changed.
 */
@Component({
  selector: 'app-options-backtest-data-check-panel',
  imports: [ButtonModule, ReceiptLabelPipe, TimestampDisplayComponent, DataCheckSessionsComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './data-check-panel.component.html',
  styleUrls: ['../options-backtest-card.scss', './data-check-panel.component.scss'],
})
export class DataCheckPanelComponent {
  readonly state = input.required<CheckState>();
  /** Whether the setup or the dates are no longer those the answer was asked with. */
  readonly stale = input(false);
  readonly dismissed = output();
  readonly retry = output();
  readonly useStretch = output<TestableStretch>();

  /** The three verdicts, each the key of its count in the answer. */
  protected readonly verdicts: readonly (keyof DataCheck['counts'])[] = ['ready', 'needs_download', 'cannot_be_tested'];

  protected readonly ready = computed(() => {
    const state = this.state();
    return state.kind === 'ready' ? state : null;
  });
  protected readonly failure = computed(() => {
    const state = this.state();
    return state.kind === 'failed' ? state.message : null;
  });
  /** The last day of the range asked: the half-open end is the midnight after it. */
  protected readonly lastDayAskedMs = computed(() => {
    const ready = this.ready();
    return ready === null ? null : ready.asked.end_ms - 1;
  });
  protected readonly stretch = computed(() => {
    const answer = this.ready()?.answer;
    return answer != null && answer.counts.cannot_be_tested > 0 ? (answer.longest_testable_stretch ?? null) : null;
  });
  protected readonly nothingTestable = computed(() => {
    const answer = this.ready()?.answer;
    return answer != null && answer.counts.cannot_be_tested > 0 && answer.longest_testable_stretch == null;
  });
}
