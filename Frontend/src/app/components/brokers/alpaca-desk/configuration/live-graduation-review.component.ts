import { ChangeDetectionStrategy, Component, input, model, output } from '@angular/core';
import { CurrencyPipe, PercentPipe } from '@angular/common';

import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import type { LiveGraduationPlan } from './live-graduation.service';

/**
 * The sealed evidence an operator reviews before the one-shot Live
 * activation (ADR 0059). Split out of `LiveGraduationComponent` to keep that
 * template under the repository's ~80-line guideline — this section is
 * fully self-contained once a plan exists.
 */
@Component({
  selector: 'app-live-graduation-review',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, PercentPipe, TimestampDisplayComponent],
  templateUrl: './live-graduation-review.component.html',
  styleUrl: './live-graduation-review.component.scss',
})
export class LiveGraduationReviewComponent {
  readonly plan = input.required<LiveGraduationPlan>();
  readonly expired = input.required<boolean>();
  readonly busy = input.required<boolean>();
  readonly applying = input.required<boolean>();
  readonly acknowledged = model.required<boolean>();

  readonly confirmRequested = output();
  readonly refreshRequested = output();
}
