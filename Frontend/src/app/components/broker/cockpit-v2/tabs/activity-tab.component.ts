// Activity tab (unified projection).
//
// The backend-owned ``/activity`` projection is the canonical execution
// view for this tab: chart fill markers, Orders Today, Broker Activity,
// and attached full-IBKR-API evidence all come from one materialized
// response so the UI cannot render an order in one surface but not another.

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
import { WorkingPendingOrdersSectionComponent } from '../reused/working-pending-orders-section/working-pending-orders-section.component';

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
  readonly selectedSessionDate = signal<string>(localDateString());
  readonly selectedResolution = signal<ChartResolution>('1m');

  readonly chartRunId = computed<string | null>(
    () => this.status().live_binding?.run_id ?? this.status().evidence_binding?.run_id ?? null,
  );

  readonly strategyInstanceId = computed<string>(() => this.status().strategy_instance_id);

  readonly activityResource = resource<
    LiveInstanceActivityProjection | null,
    {
      sid: string;
      sessionDate: string;
      resolution: ChartResolution;
      refreshKey: number;
    }
  >({
    params: () => ({
      sid: this.strategyInstanceId(),
      sessionDate: this.selectedSessionDate(),
      resolution: this.selectedResolution(),
      refreshKey: this.status().live_binding ? this.status().fetched_at_ms : 0,
    }),
    loader: ({ params }) => this.loadActivity(params.sid, params.sessionDate, params.resolution),
  });

  readonly activity = computed(() => this.activityResource.value() ?? null);
  readonly ordersToday = computed(() => this.activity()?.orders_today ?? []);
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
}
