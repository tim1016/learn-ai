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
  linkedSignal,
  resource,
} from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { BrokerActivityHealth, LiveInstanceStatus } from '../../../../api/live-instances.types';

import {
  BotTradeChartCardComponent,
  localDateString,
  type ChartSelection,
} from '../reused/bot-trade-chart-card/bot-trade-chart-card.component';
import type {
  ChartBaseResolution,
  LiveInstanceActivityProjection,
} from '../reused/bot-trade-chart-card/bot-trade-chart-card.types';
import { BrokerActivityTableComponent } from '../reused/broker-activity-table/broker-activity-table.component';
import { IncidentsPanelComponent } from '../reused/incidents-panel/incidents-panel.component';
import { LatestSignalStripComponent } from '../reused/latest-signal-strip/latest-signal-strip.component';
import {
  isOpenOrderClusterRow,
  WorkingPendingOrdersSectionComponent,
} from '../reused/working-pending-orders-section/working-pending-orders-section.component';

interface ActivityRequestParams {
  readonly sid: string;
  readonly sessionDate: string;
  readonly resolution: ChartBaseResolution;
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
  readonly strategyInstanceId = computed<string>(() => this.status().strategy_instance_id);
  readonly selectedSessionDate = linkedSignal({
    source: this.strategyInstanceId,
    computation: () => localDateString(),
  });
  readonly selectedResolution = linkedSignal<string, ChartBaseResolution>({
    source: this.strategyInstanceId,
    computation: () => '1m',
  });
  private readonly activityRequestParams = computed<ActivityRequestParams>(() => ({
    sid: this.strategyInstanceId(),
    sessionDate: this.selectedSessionDate(),
    resolution: this.selectedResolution(),
  }));

  readonly chartRunId = computed<string | null>(
    () => this.status().live_binding?.run_id ?? this.status().evidence_binding?.run_id ?? null,
  );

  readonly activityResource = resource<
    LiveInstanceActivityProjection | null,
    ActivityRequestParams
  >({
    params: () => this.activityRequestParams(),
    loader: ({ params }) => this.loadActivity(params.sid, params.sessionDate, params.resolution),
  });

  readonly activity = computed(() => this.activityResource.value() ?? null);
  readonly openOrderClusters = computed(() => openOrderClustersForProjection(this.activity()));
  readonly brokerEventSummary = computed(() => this.activity()?.broker_activity_summary ?? []);
  readonly backfillLoading = computed(() => this.activityResource.isLoading());
  readonly backfillError = computed(() => {
    const err = this.activityResource.error();
    return err instanceof Error ? err.message : err ? String(err) : null;
  });
  readonly sseStatus = computed(() => (this.activity() ? 'projection' : 'loading'));
  readonly sseError = computed<string | null>(() => null);

  /** Pass the typed, stream-fed health verdict to the server-authored notice. */
  readonly activityHealth = computed<BrokerActivityHealth | null>(
    () => this.status().operator_surface.broker_activity_health ?? null,
  );

  onChartSelectionChange(selection: ChartSelection): void {
    this.selectedSessionDate.set(selection.sessionDate);
    this.selectedResolution.set(selection.activityResolution);
  }

  private async loadActivity(
    sid: string,
    sessionDate: string,
    resolution: ChartBaseResolution,
  ): Promise<LiveInstanceActivityProjection | null> {
    if (!sid) return null;
    return firstValueFrom(
      this.http.get<LiveInstanceActivityProjection>(
        `/api/live-instances/${encodeURIComponent(sid)}/activity`,
        { params: { session_date: sessionDate, resolution } },
      ),
    );
  }
}
