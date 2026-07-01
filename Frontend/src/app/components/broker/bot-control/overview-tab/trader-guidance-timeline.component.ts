import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { LifecycleProjectionEventRow } from '../../../../api/live-instances.types';
import { fmtTimestampNy } from '../../format';

@Component({
  selector: 'app-trader-guidance-timeline',
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './trader-guidance-timeline.component.html',
  styleUrl: './trader-guidance-timeline.component.scss',
})
export class TraderGuidanceTimelineComponent {
  readonly rows = input<LifecycleProjectionEventRow[]>([]);
  readonly projectionAvailable = input<boolean>(false);
  readonly canonicalFallbackRequired = input<boolean>(true);
  readonly notice = input<string | null>(null);

  trackTimelineRow(_index: number, row: LifecycleProjectionEventRow): string {
    return row.event_id;
  }

  timelineHeadline(row: LifecycleProjectionEventRow): string {
    return row.rendered_headline ?? row.summary;
  }

  timelineTimestamp(row: LifecycleProjectionEventRow): string {
    return row.ts_ms_resolved ? fmtTimestampNy(row.ts_ms) : 'Time not available';
  }

  timelineSource(row: LifecycleProjectionEventRow): string {
    const seq = row.source_seq === null ? '' : ` #${row.source_seq}`;
    return `${row.source_type}${seq}`;
  }
}
