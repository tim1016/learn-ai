// PRD #617 — Activity tab.  One vertical scroll container; subsections
// in fixed order: Latest signal, Trade chart, Trades table, Incidents,
// Sizing audit.

import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from '@angular/core';

import type { LiveInstanceStatus } from '../../../../api/live-instances.types';

import { BotTradeChartCardComponent } from '../reused/bot-trade-chart-card/bot-trade-chart-card.component';
import { BotTradesTableComponent } from '../reused/bot-trades-table/bot-trades-table.component';
import { IncidentsPanelComponent } from '../reused/incidents-panel/incidents-panel.component';
import { LatestSignalStripComponent } from '../reused/latest-signal-strip/latest-signal-strip.component';
import { SizingAuditTableComponent } from '../reused/sizing-audit-table/sizing-audit-table.component';

@Component({
  selector: 'app-activity-tab',
  standalone: true,
  imports: [
    CommonModule,
    BotTradeChartCardComponent,
    BotTradesTableComponent,
    IncidentsPanelComponent,
    LatestSignalStripComponent,
    SizingAuditTableComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './activity-tab.component.html',
  styleUrl: './activity-tab.component.scss',
})
export class ActivityTabComponent {
  readonly status = input.required<LiveInstanceStatus>();

  readonly chartRunId = computed<string | null>(
    () => this.status().live_binding?.run_id ?? this.status().evidence_binding?.run_id ?? null,
  );

  readonly sizingRows = computed(() => this.status().sizing?.per_trade_audit ?? []);
}
