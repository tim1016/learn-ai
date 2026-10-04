import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { DataLakeProblem } from '../../../shared/data-lake';

/**
 * One panel of the Observatory: a titled surface that shows its read's
 * failure or its pending line in place of the body, so every panel reports
 * a dark data plane the same way.
 */
@Component({
  selector: 'app-lake-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrl: './lake-panel.component.scss',
  imports: [ReceiptLabelPipe],
  template: `
    <section class="panel" [attr.aria-labelledby]="headingId()">
      @if (heading(); as title) {
        <div class="panel__head">
          <h2 class="panel__title" [id]="headingId()">{{ title }}</h2>
          <ng-content select="[slot=head]" />
        </div>
      }
      @if (problem(); as issue) {
        <p class="panel__problem" role="alert">
          <span class="panel__reason">{{ issue.reason | receiptLabel }}</span>
          <span>{{ issue.message }}</span>
        </p>
      } @else if (pending(); as line) {
        <p class="panel__state" aria-live="polite" aria-busy="true">{{ line }}</p>
      } @else {
        <ng-content />
      }
    </section>
  `,
})
export class LakePanelComponent {
  /** The id of the panel's heading: its own when `heading` is set, else one inside the body. */
  readonly headingId = input.required<string>();
  /** Omit when the body brings its own heading. */
  readonly heading = input<string | null>(null);
  readonly problem = input<DataLakeProblem | null>(null);
  /** A line to show while the body's read is in flight, or null once it has landed. */
  readonly pending = input<string | null>(null);
}
