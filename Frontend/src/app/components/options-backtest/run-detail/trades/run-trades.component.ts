import { ChangeDetectionStrategy, Component, computed, inject, input, linkedSignal, output, resource } from '@angular/core';

import { extractServerMessage } from '../../../broker/operation-error';
import { TimestampDisplayPipe } from '../../../../shared/timestamp';
import type { RunDetail } from '../../options-backtest.types';
import type { DayRequest } from '../day-request';
import { RunExportComponent } from '../export/run-export.component';
import { rowsShown, TRADES_PER_PAGE } from '../paging';
import { RunDetailRefusedError, RunDetailService } from '../run-detail.service';
import { TradeTableComponent } from './trade-table.component';

/**
 * Every leg trade of a complete run, a page at a time as the server pages them. The server says how many trades there are
 * and where the next page starts; Next asks for exactly that, and Previous goes back to the page before, whose own start
 * the server gave when it was read. Nothing is cut out of a page to look like the whole. A filter by session or by leg is
 * the server's: one that names nothing comes back as its refusal, shown in its words. A read is for one run, one filter
 * and one page; when any of the three changes, an answer to the earlier one is dropped.
 */
@Component({
  selector: 'app-options-backtest-run-trades',
  imports: [TimestampDisplayPipe, RunExportComponent, TradeTableComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run-trades.component.html',
  styleUrl: './run-trades.component.scss',
})
export class RunTradesComponent {
  private readonly service = inject(RunDetailService);

  readonly run = input.required<RunDetail>();
  readonly openDay = output<DayRequest>();

  private readonly runId = computed(() => this.run().id);
  protected readonly sessionFilter = linkedSignal<number, number | null>({ source: this.runId, computation: () => null });
  protected readonly legFilter = linkedSignal<number, string>({ source: this.runId, computation: () => '' });
  /** The offset of each page the owner has gone through, newest last. The first is 0; every other was given by the server. */
  private readonly visited = linkedSignal<number, readonly number[]>({ source: this.runId, computation: () => [0] });

  /** The days of the run, from its by-day figures, to filter by. */
  protected readonly days = computed(() => this.run().figures?.by_day ?? []);
  /** The legs of the run's setup; null when the setup cannot be read, and the owner then types a leg's name. */
  protected readonly legs = computed(() => this.run().setup?.legs.map((leg) => leg.leg_id) ?? null);

  private readonly read = resource({
    params: () => ({ runId: this.runId(), session: this.sessionFilter(), leg: this.legFilter(), offset: this.visited().at(-1) ?? 0 }),
    loader: ({ params }) =>
      this.service.trades(params.runId, { sessionOpenMs: params.session, legId: params.leg, limit: TRADES_PER_PAGE, offset: params.offset }),
  });
  protected readonly page = computed(() => (this.read.hasValue() ? this.read.value() : null));
  protected readonly reading = computed(() => this.page() === null && this.read.error() === undefined);
  protected readonly problem = computed(() => {
    const error: unknown = this.read.error();
    if (error === undefined) return null;
    return error instanceof RunDetailRefusedError ? error.message : extractServerMessage(error, 'The trades could not be read.');
  });
  protected readonly where = computed(() => {
    const page = this.page();
    return page === null ? '' : rowsShown(page);
  });
  protected readonly filtered = computed(() => this.sessionFilter() !== null || this.legFilter() !== '');
  protected readonly canGoBack = computed(() => this.visited().length > 1);
  protected readonly next = computed(() => this.page()?.next_offset ?? null);

  protected chooseSession(event: Event): void {
    const select = event.target;
    if (!(select instanceof HTMLSelectElement)) return;
    // The first option is "All days"; the others are the run's days in order.
    this.sessionFilter.set(this.days()[select.selectedIndex - 1]?.session_open_ms ?? null);
    this.visited.set([0]);
  }

  protected chooseLeg(event: Event): void {
    const field = event.target;
    if (!(field instanceof HTMLSelectElement) && !(field instanceof HTMLInputElement)) return;
    this.legFilter.set(field.value.trim());
    this.visited.set([0]);
  }

  protected goNext(): void {
    const offset = this.next();
    if (offset !== null) this.visited.update((offsets) => [...offsets, offset]);
  }

  protected goBack(): void {
    this.visited.update((offsets) => offsets.slice(0, -1));
  }

  protected goFirst(): void {
    this.visited.set([0]);
  }

  protected retry(): void {
    this.read.reload();
  }
}
