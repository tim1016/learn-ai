import { ChangeDetectionStrategy, Component, ElementRef, inject, input, signal } from '@angular/core';

import type { RunDetail } from '../options-backtest.types';
import type { DayRequest } from '../run-detail/day-request';
import { SessionViewComponent } from '../run-detail/session/session-view.component';
import { RunTradesComponent } from '../run-detail/trades/run-trades.component';
import { CapitalEvidenceComponent } from './capital-evidence.component';
import { CashLadderComponent } from './cash-ladder.component';
import { RunBannerComponent } from './run-banner.component';
import { RunDayTableComponent } from './run-day-table.component';
import { RunDailyChartComponent } from './run-daily-chart.component';
import { RunDrawdownComponent } from './run-drawdown.component';
import { RunHeadlineComponent } from './run-headline.component';
import { RunMonthTableComponent } from './run-month-table.component';
import { RunLimitationsComponent } from './run-limitations.component';
import { RunRiskComponent } from './run-risk.component';
import { RunSessionsComponent } from './run-sessions.component';
import { SlippageToleranceComponent } from './slippage-tolerance.component';

/** A continuous performance desk over Python-authored figures, with click-open evidence. */
@Component({
  selector: 'app-options-backtest-run-result',
  imports: [
    RunBannerComponent,
    RunHeadlineComponent,
    RunDrawdownComponent,
    CapitalEvidenceComponent,
    RunSessionsComponent,
    SlippageToleranceComponent,
    CashLadderComponent,
    RunDayTableComponent,
    RunDailyChartComponent,
    RunMonthTableComponent,
    RunTradesComponent,
    SessionViewComponent,
    RunLimitationsComponent,
    RunRiskComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run-result.component.html',
  styleUrl: './run-result.component.scss',
})
export class RunResultComponent {
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  readonly run = input.required<RunDetail>();
  /** The day the owner last asked to look at, from a trade or from the table of days. */
  protected readonly day = signal<DayRequest | null>(null);
  protected readonly tradesOpen = signal(false);

  protected openEvidence(section: 'capital' | 'coverage' | 'risk'): void {
    const target = this.host.nativeElement.querySelector<HTMLDetailsElement>(`[data-evidence="${section}"]`);
    if (target) { target.open = true; target.scrollIntoView?.({ block: 'nearest' }); }
  }

  protected toggleTrades(event: Event): void {
    if (event.target instanceof HTMLDetailsElement && event.target.open) this.tradesOpen.set(true);
  }
}
