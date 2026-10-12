import { afterNextRender, ChangeDetectionStrategy, Component, computed, effect, ElementRef, inject, Injector, input, linkedSignal, resource, untracked, viewChild } from '@angular/core';

import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { AuthoredUsdPipe } from '../../../../shared/pipes/authored-usd.pipe';
import { TimestampDisplayComponent, TimestampDisplayPipe } from '../../../../shared/timestamp';
import { extractServerMessage } from '../../../broker/operation-error';
import type { RunDetail } from '../../options-backtest.types';
import type { DayRequest } from '../day-request';
import { RunDetailRefusedError, RunDetailService } from '../run-detail.service';
import { buildTimelinePlan } from '../timeline-plan';
import { TradeTableComponent } from '../trades/trade-table.component';
import { SessionDrawnComponent } from './session-drawn.component';
import { SessionEventsComponent } from './session-events.component';
import { TimelineChartComponent } from './timeline-chart.component';
import { TimelineLegendComponent } from './timeline-legend.component';

/**
 * One day of a complete run, closely. The owner picks a day here, or asks for one from a trade or a day of the table. The
 * stored facts come first and are always shown: how the day came out, its cash and profit, its trades, what happened in
 * it in order. Then the chart of the day with its legend, then the chart's lines and marks as text. A series the server
 * withholds is not drawn and its place says why in the server's words; nothing else is lost with it.
 *
 * The read is for one run and one day: when either changes an answer to the earlier one is dropped. Opening the view
 * moves the keyboard into it, and closing it gives the keyboard back to the control that asked.
 */
@Component({
  selector: 'app-options-backtest-session-view',
  imports: [
    AuthoredUsdPipe,
    ReceiptLabelPipe,
    TimestampDisplayComponent,
    TimestampDisplayPipe,
    TradeTableComponent,
    SessionEventsComponent,
    SessionDrawnComponent,
    TimelineChartComponent,
    TimelineLegendComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './session-view.component.html',
  styleUrl: './session-view.component.scss',
})
export class SessionViewComponent {
  private readonly service = inject(RunDetailService);
  private readonly injector = inject(Injector);

  readonly run = input.required<RunDetail>();
  /** A day asked for from outside the view (a trade, a day of the table). A new object is made for each ask. */
  readonly request = input<DayRequest | null>(null);

  private readonly runId = computed(() => this.run().id);
  /** The days of the run, to pick from. */
  protected readonly days = computed(() => this.run().figures?.by_day ?? []);
  /** The day on show: the one asked for, until the owner picks another here or closes it. */
  protected readonly chosen = linkedSignal<{ readonly runId: number; readonly request: DayRequest | null }, number | null>({
    source: () => ({ runId: this.runId(), request: this.request() }),
    computation: (source) => source.request?.openMs ?? null,
  });

  private readonly read = resource({
    params: () => {
      const openMs = this.chosen();
      return openMs === null ? undefined : { runId: this.runId(), openMs };
    },
    loader: ({ params }) => this.service.session(params.runId, params.openMs),
  });
  protected readonly session = computed(() => (this.read.hasValue() ? this.read.value() : null));
  protected readonly reading = computed(() => this.chosen() !== null && this.session() === null && this.read.error() === undefined);
  protected readonly problem = computed(() => {
    const error: unknown = this.read.error();
    if (error === undefined) return null;
    return error instanceof RunDetailRefusedError ? error.message : extractServerMessage(error, 'The day could not be read.');
  });
  protected readonly plan = computed(() => {
    const session = this.session();
    return session === null ? null : buildTimelinePlan(session);
  });

  private readonly heading = viewChild.required<ElementRef<HTMLElement>>('heading');
  private readonly picker = viewChild.required<ElementRef<HTMLSelectElement>>('picker');

  constructor() {
    // A day asked for from outside moves the keyboard into the view, to its heading.
    effect(() => {
      if (this.request() !== null) untracked(() => afterNextRender(() => this.heading().nativeElement.focus(), { injector: this.injector }));
    });
  }

  protected choose(event: Event): void {
    const select = event.target;
    if (!(select instanceof HTMLSelectElement)) return;
    // The first option is "Choose a day"; the others are the run's days in order.
    this.chosen.set(this.days()[select.selectedIndex - 1]?.session_open_ms ?? null);
  }

  /** Puts the day away and gives the keyboard back to the control that asked for it, or to the picker if that is gone. */
  protected close(): void {
    const origin = this.request()?.origin ?? null;
    this.chosen.set(null);
    afterNextRender(
      () => {
        if (origin !== null && origin.isConnected) origin.focus();
        else this.picker().nativeElement.focus();
      },
      { injector: this.injector },
    );
  }

  protected retry(): void {
    this.read.reload();
  }
}
