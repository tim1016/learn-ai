import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { LaunchProblem } from '../run-launch';

/**
 * Something about the run that is not on screen: a backtest that was not started (the server's own words and code), one
 * that was started whose record cannot be read yet (said calmly, for the page is looking again), or a check of whether a
 * run is going that could not be made, which can be tried again.
 */
@Component({
  selector: 'app-options-backtest-launch-notice',
  imports: [ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="notice" [class.notice--error]="problem().kind !== 'record_unread'" [attr.role]="problem().kind === 'record_unread' ? 'status' : 'alert'">
      <p>{{ problem().message }}</p>
      @if (problem().code; as code) { <p class="code">{{ code | receiptLabel }}</p> }
      @if (problem().kind === 'unchecked') { <button type="button" class="again" (click)="retry.emit()">Try again</button> }
    </div>
  `,
  styles: `
    :host { display: block; }
    p { margin: 0; }
    .notice { border: 1px solid var(--border-light); border-radius: var(--radius); background: var(--bg-surface); color: var(--text-secondary); padding: var(--space-3); font-size: var(--fs-sm); display: grid; gap: var(--space-2); }
    .notice--error { border-color: var(--bear); color: var(--bear); }
    .code { font-size: var(--fs-xs); color: var(--text-secondary); }
    .again { justify-self: start; background: none; border: 1px solid var(--border-strong); color: var(--text-secondary); border-radius: var(--radius); padding: var(--space-1) var(--space-3); font: inherit; font-size: var(--fs-sm); cursor: pointer; }
    .again:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  `,
})
export class LaunchNoticeComponent {
  readonly problem = input.required<LaunchProblem>();
  readonly retry = output();
}
