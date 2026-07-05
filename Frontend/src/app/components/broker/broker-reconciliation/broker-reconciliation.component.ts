import {
  ChangeDetectionStrategy,
  Component,
  Injector,
  OnDestroy,
  computed,
  effect,
  inject,
  runInInjectionContext,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { PageHeaderComponent } from '../../../shared/page-header/page-header.component';
import { AccountTruthBoardComponent } from '../account-truth-board/account-truth-board.component';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { BrokerService } from '../../../services/broker.service';
import { brokerSse, type SseStream } from '../../../services/broker-sse';
import type {
  AccountTruthResponse,
  IbkrAccountSummary,
  IbkrPnLTick,
  IbkrPosition,
  IbkrPositionsSnapshot,
} from '../../../api/broker-models';
import {
  diffBps,
  fmtBrokerExpiryDate,
  fmtCurrency,
  fmtNumber,
  fmtSignedCurrency,
  toleranceBand,
  type ToleranceBand,
} from '../format';

const ACCOUNT_REFRESH_MS = 5_000;

interface GreeksRow {
  position: IbkrPosition;
  ibkrDelta: number | null;
  engineDelta: number | null;
  deltaBps: number | null;
  deltaBand: ToleranceBand | null;
  ibkrUnrealized: number | null;
  ibkrPosition: number | null;
}

interface AccountReconcileRow {
  metric: string;
  ibkr: number | null;
  engine: number | null;
  diff: number | null;
  band: ToleranceBand | null;
}

/**
 * /broker/reconciliation — IBKR vs engine side-by-side reconciliation.
 *
 * V1 scope (per ``ibkr-frontend-implementation-plan.md`` §9.5 "build
 * incrementally"):
 *   * Per-position Greeks: IBKR delta from the per-position P&L stream
 *     joined with QuantLib's analytic_bs delta.
 *   * Account-level reconciliation table with the IBKR side wired and
 *     the engine column as a typed placeholder. The .NET PortfolioService
 *     fan-in is a follow-up — captured in the row's ``engine === null``
 *     state so the diff column shows ``—``.
 *   * Per-fill reconciliation deferred (per plan).
 *
 * The "Export CSV" and "Add note" actions are local-only stubs (the
 * Phase 4.5 backend endpoints don't exist yet). "Add note" copies the
 * focused row to the clipboard so a human can paste it into
 * ``docs/math-sources-of-truth.md``.
 */
@Component({
  selector: 'app-broker-reconciliation',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PageHeaderComponent, RouterLink, AccountTruthBoardComponent],
  styleUrl: './broker-reconciliation.component.scss',
  templateUrl: './broker-reconciliation.component.html',
})
export class BrokerReconciliationComponent implements OnDestroy {
  private readonly broker = inject(BrokerService);
  private readonly health = inject(BrokerHealthService);
  readonly bannerState = this.health.bannerState;
  private readonly injector = inject(Injector);

  readonly positionsSnapshot = signal<IbkrPositionsSnapshot | null>(null);
  readonly account = signal<IbkrAccountSummary | null>(null);
  readonly accountTruth = signal<AccountTruthResponse | null>(null);
  readonly loadError = signal<string | null>(null);

  private readonly pnlStream = signal<SseStream<IbkrPnLTick> | null>(null);
  private readonly perConIdTick = signal<Map<number, IbkrPnLTick>>(new Map());

  /**
   * Engine deltas keyed by ``con_id``. Recomputed when (a) the account
   * snapshot or positions list refreshes, (b) the IBKR per-position
   * tick arrives. We do not chase microscopic IV changes — the V1 path
   * uses a single QuantLib call per position per refresh.
   */
  private readonly engineDeltaByConId = signal<Map<number, number>>(new Map());

  private accountRefreshTimer: ReturnType<typeof setInterval> | null = null;

  readonly canStream = computed(() => this.health.health()?.connected === true);

  readonly greeksRows = computed<GreeksRow[]>(() => {
    const snap = this.positionsSnapshot();
    if (snap === null) return [];
    const ticks = this.perConIdTick();
    const engine = this.engineDeltaByConId();
    return snap.positions
      .filter((p) => p.sec_type === 'OPT')
      .map((position) => {
        const tick = ticks.get(position.con_id) ?? null;
        const ibkrDelta = null; // IBKR per-position stream doesn't carry Greeks; left null deliberately.
        const engineDelta = engine.get(position.con_id) ?? null;
        const bps = diffBps(ibkrDelta, engineDelta);
        return {
          position,
          ibkrDelta,
          engineDelta,
          deltaBps: bps,
          deltaBand: toleranceBand(bps),
          ibkrUnrealized: tick?.unrealized_pnl ?? null,
          ibkrPosition: tick?.position ?? null,
        };
      });
  });

  readonly accountRows = computed<AccountReconcileRow[]>(() => {
    const a = this.account();
    if (a === null) return [];
    // Engine values are intentionally null — wiring to the .NET
    // PortfolioService is a follow-up. The row layout is locked in so
    // future work just plugs the numbers in.
    const make = (label: string, ibkr: number | null, eng: number | null): AccountReconcileRow => {
      if (ibkr === null || eng === null) {
        return { metric: label, ibkr, engine: eng, diff: null, band: null };
      }
      const diff = ibkr - eng;
      const bps = diffBps(ibkr, eng);
      return { metric: label, ibkr, engine: eng, diff, band: toleranceBand(bps) };
    };
    return [
      make('Cash', a.cash_balance ?? null, null),
      make('Net liquidation', a.net_liquidation ?? null, null),
      make('Day P&L', a.day_pnl ?? null, null),
      make('Unrealized', a.unrealized_pnl ?? null, null),
      make('Realized', a.realized_pnl ?? null, null),
    ];
  });

  readonly fmtCurrency = fmtCurrency;
  readonly fmtSignedCurrency = fmtSignedCurrency;
  readonly fmtNumber = fmtNumber;
  readonly fmtBrokerExpiryDate = fmtBrokerExpiryDate;

  constructor() {
    void this.refresh();

    // Roll up the per-position stream into a Map keyed by con_id so
    // the row template can read latest-tick-wins.
    effect(() => {
      const stream = this.pnlStream();
      if (stream === null) return;
      const data = stream.data();
      if (data.length === 0) return;
      this.perConIdTick.update((prev) => {
        const next = new Map(prev);
        for (const t of data) {
          if (t.con_id !== null) next.set(t.con_id, t);
        }
        return next;
      });
    });

    this.accountRefreshTimer = setInterval(
      () => void this.refreshAccountOnly(),
      ACCOUNT_REFRESH_MS,
    );
  }

  ngOnDestroy(): void {
    if (this.accountRefreshTimer !== null) {
      clearInterval(this.accountRefreshTimer);
      this.accountRefreshTimer = null;
    }
    this.pnlStream()?.close();
  }

  async refresh(): Promise<void> {
    if (!this.canStream()) return;
    this.loadError.set(null);
    try {
      const [positionsSnap, accountSummary, truth] = await Promise.all([
        this.broker.positions(),
        this.broker.account(),
        this.broker.accountTruth(),
      ]);
      this.positionsSnapshot.set(positionsSnap);
      this.account.set(accountSummary);
      this.accountTruth.set(truth);
      this.openPnLStream(positionsSnap.positions);
      void this.repriceAllPositions(positionsSnap.positions);
    } catch (err) {
      this.loadError.set(extractMessage(err));
    }
  }

  async refreshAccountOnly(): Promise<void> {
    if (!this.canStream()) return;
    try {
      this.account.set(await this.broker.account());
    } catch {
      // Soft-failure: keep the previous snapshot, the next tick will
      // try again. The error is surfaced via the global health banner
      // poll, which will flip to disconnected if the broker is down.
    }
  }

  exportCsv(): void {
    const rows: string[] = ['section,metric,ibkr,engine,diff_bps'];
    for (const r of this.accountRows()) {
      // Account rows store the dollar diff and a tolerance band but
      // not the raw bps. Recompute bps here so the CSV column matches
      // its header (consumers expect a number, not 'green'/'yellow'/'red').
      const bps = diffBps(r.ibkr, r.engine);
      rows.push(
        ['account', r.metric, r.ibkr ?? '', r.engine ?? '', bps ?? '']
          .map(csvCell)
          .join(','),
      );
    }
    for (const r of this.greeksRows()) {
      const p = r.position;
      rows.push(
        [
          'greeks',
          `${p.symbol} ${p.right ?? ''} ${p.strike ?? ''}`,
          r.ibkrDelta ?? '',
          r.engineDelta ?? '',
          r.deltaBps ?? '',
        ]
          .map(csvCell)
          .join(','),
      );
    }
    const blob = new Blob([rows.join('\n')], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `broker-reconciliation-${Date.now()}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  async addNote(row: AccountReconcileRow | GreeksRow): Promise<void> {
    const text = JSON.stringify(row, null, 2);
    try {
      await navigator.clipboard.writeText(text);
      window.alert('Row copied to clipboard. Paste into docs/math-sources-of-truth.md.');
    } catch {
      window.prompt('Copy this row manually:', text);
    }
  }

  trackGreeks = (_: number, row: GreeksRow): number => row.position.con_id;
  trackAccount = (_: number, row: AccountReconcileRow): string => row.metric;

  private openPnLStream(positions: IbkrPosition[]): void {
    const existing = this.pnlStream();
    if (existing) existing.close();
    if (positions.length === 0) {
      this.pnlStream.set(null);
      return;
    }
    const conIds = positions
      .map((p) => p.con_id)
      .filter((c): c is number => Number.isFinite(c));
    const query = conIds.map((c) => `con_ids=${c}`).join('&');
    const stream = runInInjectionContext(this.injector, () =>
      brokerSse<IbkrPnLTick>(
        `/api/broker/pnl/positions/stream?${query}&debounce_ms=1000`,
        'pnl',
        { maxBuffer: positions.length * 60 },
      ),
    );
    this.pnlStream.set(stream);
  }

  private async repriceAllPositions(_positions: IbkrPosition[]): Promise<void> {
    // Engine delta requires the **underlying** spot, not the option's
    // own mark. ``IbkrPosition.market_price`` for an option position is
    // the option premium per IBKR — feeding it into Black-Scholes as
    // ``spot`` produces nonsense deltas. Until the reconciliation page
    // joins against the live chain stream (so we can read the
    // underlying's marketPrice from the chain snapshot), leave the
    // engine delta column empty rather than display wrong numbers.
    // Tracked as a follow-up — see the caveat in the template and the
    // V1 scope note in this component's class docstring.
    this.engineDeltaByConId.set(new Map());
  }
}

function csvCell(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '';
  const s = String(value);
  if (s.includes(',') || s.includes('"') || s.includes('\n')) {
    return `"${s.replace(/"/g, '""')}"`;
  }
  return s;
}

function extractMessage(err: unknown): string {
  if (err == null) return 'Unknown error';
  if (typeof err === 'string') return err;
  if (typeof err === 'object' && 'message' in err) {
    return String((err as { message: unknown }).message);
  }
  return 'Unknown error';
}
