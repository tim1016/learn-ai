// Activity tab (stream-first projections).
// Deprecated with the legacy tab surface. The per-bot lifecycle
// workbench may embed it temporarily because this tab is read-only; new
// workbench-specific behavior belongs in workbench-owned components.
//
// The Bot event stream is the canonical historical surface. The
// backend-owned ``/activity`` response remains only for charting plus
// secondary projections that need richer broker/order facts: open order
// clusters and the broker tail.

import { CommonModule } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
  signal,
} from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { BrokerActivityHealth, LiveInstanceStatus } from '../../../../api/live-instances.types';

import { BotEventStreamComponent } from '../reused/bot-event-stream/bot-event-stream.component';
import {
  BotTradeChartCardComponent,
  type ChartResolution,
  localDateString,
  type ChartSelection,
} from '../reused/bot-trade-chart-card/bot-trade-chart-card.component';
import type { LiveInstanceActivityProjection } from '../reused/bot-trade-chart-card/bot-trade-chart-card.types';
import { BrokerActivityTableComponent } from '../reused/broker-activity-table/broker-activity-table.component';
import { IncidentsPanelComponent } from '../reused/incidents-panel/incidents-panel.component';
import { LatestSignalStripComponent } from '../reused/latest-signal-strip/latest-signal-strip.component';
import {
  isOpenOrderClusterRow,
  WorkingPendingOrdersSectionComponent,
} from '../reused/working-pending-orders-section/working-pending-orders-section.component';

export function activityRefreshKeyForStatus(status: LiveInstanceStatus): number | null {
  if (!status.live_binding) return 0;
  const controlPlane = status.operator_surface.control_plane;
  if (
    controlPlane !== null &&
    controlPlane.state !== 'CONNECTED' &&
    controlPlane.state !== 'RETRYING'
  ) {
    return null;
  }
  return status.fetched_at_ms;
}

interface ActivityRequestParams {
  readonly sid: string;
  readonly sessionDate: string;
  readonly resolution: ChartResolution;
  readonly refreshKey: number | null;
}

interface CachedActivity {
  readonly sid: string;
  readonly sessionDate: string;
  readonly resolution: ChartResolution;
  readonly projection: LiveInstanceActivityProjection | null;
}

export function cachedActivityForRequest(
  cached: CachedActivity | null,
  params: ActivityRequestParams,
): LiveInstanceActivityProjection | null | undefined {
  if (
    cached === null ||
    cached.sid !== params.sid ||
    cached.sessionDate !== params.sessionDate ||
    cached.resolution !== params.resolution
  ) {
    return undefined;
  }
  return cached.projection;
}

export function activityProjectionForDisplay(
  resourceValue: LiveInstanceActivityProjection | null | undefined,
  cached: CachedActivity | null,
  params: ActivityRequestParams,
): LiveInstanceActivityProjection | null {
  if (resourceValue !== undefined) return resourceValue;
  return cachedActivityForRequest(cached, params) ?? null;
}

export function openOrderClustersForProjection(
  projection: LiveInstanceActivityProjection | null,
): LiveInstanceActivityProjection['orders_today'] {
  return projection?.orders_today.filter(isOpenOrderClusterRow) ?? [];
}

@Component({
  selector: 'app-activity-tab',
  imports: [
    CommonModule,
    BotEventStreamComponent,
    BotTradeChartCardComponent,
    BrokerActivityTableComponent,
    IncidentsPanelComponent,
    LatestSignalStripComponent,
    WorkingPendingOrdersSectionComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './activity-tab.component.html',
  styleUrl: './activity-tab.component.scss',
})
export class ActivityTabComponent {
  readonly status = input.required<LiveInstanceStatus>();

  private readonly http = inject(HttpClient);
  readonly selectedSessionDate = signal<string>(localDateString());
  readonly selectedResolution = signal<ChartResolution>('1m');
  private readonly cachedActivity = signal<CachedActivity | null>(null);
  private readonly activityRequestParams = computed<ActivityRequestParams>(() => ({
    sid: this.strategyInstanceId(),
    sessionDate: this.selectedSessionDate(),
    resolution: this.selectedResolution(),
    refreshKey: this.activityRefreshKey(),
  }));

  readonly chartRunId = computed<string | null>(
    () => this.status().live_binding?.run_id ?? this.status().evidence_binding?.run_id ?? null,
  );

  readonly strategyInstanceId = computed<string>(() => this.status().strategy_instance_id);

  readonly activityRefreshKey = computed<number | null>(() => {
    return activityRefreshKeyForStatus(this.status());
  });

  readonly activityResource = resource<
    LiveInstanceActivityProjection | null,
    ActivityRequestParams
  >({
    params: () => this.activityRequestParams(),
    loader: ({ params }): Promise<LiveInstanceActivityProjection | null> => {
      if (params.refreshKey === null) {
        const cached = cachedActivityForRequest(this.cachedActivity(), params);
        if (cached !== undefined) return Promise.resolve(cached);
      }
      return this.loadAndCacheActivity(params.sid, params.sessionDate, params.resolution);
    },
  });

  readonly activity = computed(() => {
    return activityProjectionForDisplay(
      this.activityResource.value(),
      this.cachedActivity(),
      this.activityRequestParams(),
    );
  });
  readonly openOrderClusters = computed(() => openOrderClustersForProjection(this.activity()));
  readonly brokerEventRows = computed(() => this.activity()?.broker_activity_rows ?? []);
  readonly backfillLoading = computed(() => this.activityResource.isLoading());
  readonly backfillError = computed(() => {
    const err = this.activityResource.error();
    return err instanceof Error ? err.message : err ? String(err) : null;
  });
  readonly sseStatus = computed(() => (this.activity() ? 'projection' : 'loading'));
  readonly sseError = computed<string | null>(() => null);

  /** PR 5 — pass the typed health verdict from the 4s status poll to the
   *  table so it can replace the implicit spinner with a server-authored
   *  notice. Null until the first status response arrives. */
  readonly activityHealth = computed<BrokerActivityHealth | null>(
    () => this.status().operator_surface.broker_activity_health ?? null,
  );

  onChartSelectionChange(selection: ChartSelection): void {
    this.selectedSessionDate.set(selection.sessionDate);
    this.selectedResolution.set(selection.resolution);
  }

  private async loadActivity(
    sid: string,
    sessionDate: string,
    resolution: ChartResolution,
  ): Promise<LiveInstanceActivityProjection | null> {
    if (!sid) return null;
    return firstValueFrom(
      this.http.get<LiveInstanceActivityProjection>(
        `/api/live-instances/${encodeURIComponent(sid)}/activity`,
        { params: { session_date: sessionDate, resolution } },
      ),
    );
  }

  private async loadAndCacheActivity(
    sid: string,
    sessionDate: string,
    resolution: ChartResolution,
  ): Promise<LiveInstanceActivityProjection | null> {
    const projection = await this.loadActivity(sid, sessionDate, resolution);
    this.cachedActivity.set({ sid, sessionDate, resolution, projection });
    return projection;
  }
}
