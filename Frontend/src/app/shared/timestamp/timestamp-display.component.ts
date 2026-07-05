import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import {
  formatTimestampDisplay,
  type TimestampDisplayMode,
  type TimestampGranularity,
} from './timestamp-display';

@Component({
  selector: 'app-timestamp-display',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <ng-content select="[timestamp-prefix]" />
    {{ text() }}
    <ng-content select="[timestamp-suffix]" />
  `,
  host: {
    '[attr.data-timestamp-mode]': 'mode()',
  },
})
export class TimestampDisplayComponent {
  readonly value = input<number | null | undefined>(null);
  readonly mode = input<TimestampDisplayMode>('local');
  readonly granularity = input<TimestampGranularity>('datetime');
  readonly localTimeZone = input<string | undefined>(undefined);
  readonly fallback = input('—');

  protected readonly text = computed(() => formatTimestampDisplay(this.value(), {
    mode: this.mode(),
    granularity: this.granularity(),
    localTimeZone: this.localTimeZone(),
    fallback: this.fallback(),
  }));
}
