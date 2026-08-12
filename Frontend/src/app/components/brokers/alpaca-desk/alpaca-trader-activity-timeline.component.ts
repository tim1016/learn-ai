import { CurrencyPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { Timeline } from 'primeng/timeline';

import type { BrokerActivity } from '../../../api/alpaca.types';
import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';

function eventTime(activity: BrokerActivity): number {
  return activity.occurred_at_ms ?? activity.observed_at_ms;
}

/** A chronological projection of raw account activity without creating trader prose. */
@Component({
  selector: 'app-alpaca-trader-activity-timeline',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AssetIdentityComponent,
    CurrencyPipe,
    ReceiptLabelPipe,
    Timeline,
    TimestampDisplayComponent,
  ],
  templateUrl: './alpaca-trader-activity-timeline.component.html',
  styleUrl: './alpaca-trader-activity-timeline.component.scss',
})
export class AlpacaTraderActivityTimelineComponent {
  readonly activities = input<readonly BrokerActivity[] | undefined>(undefined);
  readonly unavailable = input(false);

  protected readonly rows = computed(() =>
    [...(this.activities() ?? [])].sort((left, right) => eventTime(right) - eventTime(left)),
  );

  protected readonly timelineAccessibility = {
    host: { role: 'list', 'aria-label': 'Today at the desk activity' },
    event: { role: 'listitem' },
  };
}
