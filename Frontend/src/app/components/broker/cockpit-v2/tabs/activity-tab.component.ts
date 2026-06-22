// Activity tab (ADR 0014 / PRD #617).
//
// The broker-activity surface is now the canonical execution view: rows
// are authored by the backend publisher and rendered verbatim by
// ``BrokerActivityTableComponent`` + ``WorkingPendingOrdersSectionComponent``.
// The previous ``SizingAuditTableComponent`` is deleted — its provenance
// now lives in the row drill-down's ``engine_overlay.sizing_provenance``
// (per ADR 0014 §7).
//
// Latest signal + Trade chart + Incidents remain on this tab; they're
// different domains (decision feed, price chart, operational health),
// not execution-narrative surfaces.

import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  Injector,
  computed,
  effect,
  inject,
  input,
  runInInjectionContext,
  signal,
} from '@angular/core';

import type { LiveInstanceStatus } from '../../../../api/live-instances.types';

import { BotTradeChartCardComponent } from '../reused/bot-trade-chart-card/bot-trade-chart-card.component';
import { BrokerActivityTableComponent } from '../reused/broker-activity-table/broker-activity-table.component';
import {
  brokerActivityStream,
  type BrokerActivityStream,
} from '../reused/broker-activity-table/broker-activity-stream';
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

  private readonly injector = inject(Injector);

  readonly chartRunId = computed<string | null>(
    () => this.status().live_binding?.run_id ?? this.status().evidence_binding?.run_id ?? null,
  );

  readonly strategyInstanceId = computed<string>(() => this.status().strategy_instance_id);

  // The broker-activity stream is owned at the tab level so both the
  // executed-trades table and the working/pending panel render from the
  // same authored row sequence. We tear it down + re-bootstrap whenever
  // the strategy_instance_id changes.
  private readonly stream = signal<BrokerActivityStream | null>(null);

  readonly activityRows = computed(() => this.stream()?.rows() ?? []);
  readonly backfillLoading = computed(() => this.stream()?.backfillLoading() ?? true);
  readonly backfillError = computed(() => this.stream()?.backfillError() ?? null);
  readonly sseStatus = computed(() => this.stream()?.sseStatus() ?? 'connecting');
  readonly sseError = computed(() => this.stream()?.sseError() ?? null);

  constructor() {
    effect(() => {
      const sid = this.strategyInstanceId();
      const prev = this.stream();
      if (prev !== null) prev.close();
      const next = runInInjectionContext(this.injector, () => brokerActivityStream(sid));
      this.stream.set(next);
    });
  }
}
