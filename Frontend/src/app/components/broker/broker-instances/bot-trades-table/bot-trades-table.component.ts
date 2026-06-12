import { HttpClient } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  input,
  resource,
} from '@angular/core';
import { firstValueFrom } from 'rxjs';
import type {
  ExecutionRow,
  TradeRow,
} from '../bot-trade-chart-card/bot-trade-chart-card.types';

const POLL_INTERVAL_MS = 5_000;
const NY_TZ = 'America/New_York';

/** A trade row joined with the executions that filled its entry/exit. */
interface JoinedTradeRow extends TradeRow {
  qty: number;
  fees: number;
  entryExecIds: string[];
  exitExecIds: string[];
}

@Component({
  selector: 'app-bot-trades-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './bot-trades-table.component.html',
  styleUrl: './bot-trades-table.component.scss',
})
export class BotTradesTableComponent {
  readonly runId = input<string | null>(null);

  private readonly http = inject(HttpClient);
  private readonly destroyRef = inject(DestroyRef);

  protected readonly tradesResource = resource<TradeRow[], string | null>({
    params: () => this.runId(),
    loader: ({ params }) => this.loadTrades(params),
  });

  protected readonly executionsResource = resource<ExecutionRow[], string | null>({
    params: () => this.runId(),
    loader: ({ params }) => this.loadExecutions(params),
  });

  protected readonly joinedRows = computed<JoinedTradeRow[]>(() => {
    const trades = this.tradesResource.value() ?? [];
    const execs = this.executionsResource.value() ?? [];
    return trades.map((t) => this.joinTrade(t, execs));
  });

  protected readonly hasData = computed<boolean>(
    () => this.joinedRows().length > 0,
  );

  protected readonly totalPnl = computed<number>(() =>
    this.joinedRows().reduce((acc, r) => acc + r.pnl_points * r.qty, 0),
  );

  constructor() {
    const timer = setInterval(() => {
      this.tradesResource.reload();
      this.executionsResource.reload();
    }, POLL_INTERVAL_MS);
    this.destroyRef.onDestroy(() => clearInterval(timer));
  }

  private async loadTrades(runId: string | null): Promise<TradeRow[]> {
    if (!runId) return [];
    return firstValueFrom(
      this.http.get<TradeRow[]>(`/api/live-runs/${encodeURIComponent(runId)}/trades`),
    );
  }

  private async loadExecutions(runId: string | null): Promise<ExecutionRow[]> {
    if (!runId) return [];
    return firstValueFrom(
      this.http.get<ExecutionRow[]>(
        `/api/live-runs/${encodeURIComponent(runId)}/executions`,
      ),
    );
  }

  /** Pair a trade row with the executions that fall on its entry/exit
   * timestamps. The engine writes one entry fill and one exit fill per
   * trade in deployment_validation; broader strategies with partial fills
   * accumulate qty and fees across multiple execs per side. */
  private joinTrade(trade: TradeRow, execs: ExecutionRow[]): JoinedTradeRow {
    let qty = 0;
    let fees = 0;
    const entryExecIds: string[] = [];
    const exitExecIds: string[] = [];
    for (const e of execs) {
      if (e.ts_ms === trade.entry_time_ms) {
        qty += Math.abs(e.fill_quantity);
        fees += e.fee;
        entryExecIds.push(e.exec_id);
      } else if (e.ts_ms === trade.exit_time_ms) {
        fees += e.fee;
        exitExecIds.push(e.exec_id);
      }
    }
    return { ...trade, qty, fees, entryExecIds, exitExecIds };
  }

  protected fmtTimeNy(ms: number | null | undefined): string {
    if (ms === null || ms === undefined) return '—';
    return new Intl.DateTimeFormat('en-US', {
      timeZone: NY_TZ,
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    }).format(new Date(ms));
  }

  protected fmtPrice(p: number): string {
    return p.toFixed(2);
  }

  protected fmtSignedPnl(p: number): string {
    const sign = p >= 0 ? '+' : '';
    return `${sign}${p.toFixed(2)}`;
  }

  protected trackTrade(_i: number, t: JoinedTradeRow): number {
    return t.entry_time_ms;
  }
}
