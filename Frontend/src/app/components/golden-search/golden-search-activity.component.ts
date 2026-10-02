import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { TradeActivity } from './golden-search.types';

/** Scheduled-session minimums from Python, frozen with the plan (ADR 0074). */
@Component({
  selector: 'app-golden-search-activity',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-activity.component.html',
  styleUrl: './golden-search-activity.component.scss',
})
export class GoldenSearchActivityComponent {
  readonly activity = input.required<TradeActivity>();
  protected readonly windows = computed(() => this.activity().windows.filter((window) => !window.key.startsWith('training_')));
  protected readonly training = computed(() => this.activity().windows.filter((window) => window.key.startsWith('training_')));
}
