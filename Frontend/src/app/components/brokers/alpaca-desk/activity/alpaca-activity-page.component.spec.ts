import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import axe from 'axe-core';
import { of } from 'rxjs';
import { describe, expect, it, vi } from 'vitest';

import type {
  ActivityPeriod,
  ActivityPeriodStatement,
  BrokerActivity,
  PeriodFeeRow,
  PeriodFees,
  PortfolioHistoryProof,
} from '../../../../api/alpaca.types';
import { resourceTarget } from '../../../../fleet/resource-target';
import { BrokersService } from '../../../../services/brokers.service';
import { AlpacaDeskAccountDataService } from '../alpaca-desk-account-data.service';
import { ALPACA_PORTFOLIO_HISTORY_CHART_FACTORY } from '../alpaca-portfolio-history-chart.component';
import { AlpacaActivityPageComponent } from './alpaca-activity-page.component';

const TARGET = resourceTarget('alpaca', 'clrk_spec', { accountId: 'PA1', bindingGeneration: 1, routingEpoch: 1 });
const FENCE = { bindingGeneration: 1, routingEpoch: 1 };
const OBSERVED_AT_MS = 1_790_000_000_000;

function botRow(sid: string, total: string): PeriodFeeRow {
  return {
    subject_id: `bot:${sid}`, strategy_instance_id: sid, label: sid, order_id: null,
    estimated_usd: total, modelled_settled_usd: '0', observed_usd: '0', total_usd: total,
  };
}

const OUTSIDE_ROW: PeriodFeeRow = {
  subject_id: 'external:ord-7f3a', strategy_instance_id: null, label: 'Outside order · MSFT', order_id: 'ord-7f3a',
  estimated_usd: '0', modelled_settled_usd: '0', observed_usd: '0.05', total_usd: '0.05',
};

/** Python-authored strings, exactly as the data plane sends them. */
const TODAY_STATEMENT: ActivityPeriodStatement = {
  state: 'ready', detail: null, realized_usd: '12.34', fees_usd: '0.10', open_usd: '-3.00', net_usd: '9.24',
};

/** Each period's own fee rows: the bots that paid fees in that period only. */
const PERIOD_ROWS: Readonly<Record<ActivityPeriod, readonly PeriodFeeRow[]>> = {
  today: [botRow('spy-ema-20260929-0930', '0.10')],
  '30d': [botRow('spy-ema-20260929-0930', '0.10'), botRow('paper-ema-spy-0924', '0.44'), OUTSIDE_ROW],
  '60d': [botRow('spy-ema-20260929-0930', '0.10'), botRow('paper-ema-spy-0924', '0.44'), botRow('paper-ema-spy-0801', '0.31')],
};

function periodFees(period: ActivityPeriod): PeriodFees {
  return {
    account_id: 'PA1', observed_at_ms: OBSERVED_AT_MS, authority_revision: 3, available: true, known: true,
    account_unattributed_usd: '0', messages: [], rows: [...PERIOD_ROWS[period]],
    period, period_start_ms: OBSERVED_AT_MS - 86_400_000,
    statement: period === 'today' ? TODAY_STATEMENT : { ...TODAY_STATEMENT, realized_usd: '40.00', net_usd: '36.90' },
  };
}

function fill(): BrokerActivity {
  return {
    broker: 'alpaca', activity_id: 'fill-1', native_order_id: 'order-1', activity_type: 'FILL',
    category: 'trade_activity', symbol: 'SPY', side: 'buy', quantity: 12, price: 668.12, net_amount: -8_017.44,
    occurred_at_ms: OBSERVED_AT_MS - 60_000, observed_at_ms: OBSERVED_AT_MS - 60_000,
  };
}

function proof(): PortfolioHistoryProof {
  return {
    history: { timestamps: [], equity: [], profit_loss: [], base_value: 100_000, timeframe: '1D' },
    attribution: null,
    reconciliation: null,
    proof_unavailable_reason: 'Alpaca reported no account values for this period.',
  };
}

function brokers() {
  return {
    getPeriodFees: vi.fn((_target: unknown, period: ActivityPeriod) => Promise.resolve(periodFees(period))),
    listActivities: vi.fn().mockResolvedValue([fill()]),
    getPortfolioHistoryProof: vi.fn().mockResolvedValue(proof()),
    getCustodyDiagnosis: vi.fn().mockResolvedValue({
      broker: 'alpaca', account_id: 'PA1', authority_kind: 'real_paper', in_sync: true,
      observed_at_ms: OBSERVED_AT_MS, snapshot_version: 'v1', resolution_posture: 'paper', resolvable: false,
      blocked_reason: null, divergences: [], resolution_plan: [],
    }),
    getSqliteClerkProjection: vi.fn().mockRejectedValue(new Error('records offline')),
    getSqliteClerkTimeline: vi.fn().mockRejectedValue(new Error('records offline')),
    accountTransactions: vi.fn().mockRejectedValue(new Error('records offline')),
  };
}

async function renderActivity(options: { query?: Record<string, string>; broker?: ReturnType<typeof brokers> } = {}) {
  const broker = options.broker ?? brokers();
  const queryParamMap = convertToParamMap(options.query ?? {});
  const view = await render(AlpacaActivityPageComponent, {
    providers: [
      provideRouter([]),
      { provide: ActivatedRoute, useValue: { queryParamMap: of(queryParamMap), snapshot: { queryParamMap } } },
      // Provided by the account workspace in the app.
      {
        provide: AlpacaDeskAccountDataService,
        useValue: {
          target: () => TARGET,
          accountId: () => 'PA1',
          fence: () => FENCE,
          clerkStatus: {
            hasValue: () => true,
            value: () => ({ latest_reconciliation: { verdict: 'clean', recorded_at_ms: OBSERVED_AT_MS } }),
            error: () => undefined,
            isLoading: () => false,
          },
        },
      },
      { provide: BrokersService, useValue: broker },
      {
        provide: ALPACA_PORTFOLIO_HISTORY_CHART_FACTORY,
        useValue: vi.fn(() => ({
          addSeries: vi.fn().mockReturnValue({ setData: vi.fn() }),
          applyOptions: vi.fn(),
          remove: vi.fn(),
          timeScale: vi.fn().mockReturnValue({ fitContent: vi.fn() }),
        })),
      },
    ],
  });
  return { broker, view };
}

/** The amount a statement line shows, read from its own `<dd>`. */
function statementAmount(term: string): string {
  const dt = screen.getByText(term, { selector: 'dt' });
  return dt.nextElementSibling?.textContent?.trim() ?? '';
}

function feeTable(): HTMLElement {
  return screen.getByRole('table', { name: /^Fees per bot/ });
}

function period(name: string): HTMLElement {
  return within(screen.getByRole('radiogroup', { name: 'Activity period' })).getByRole('radio', { name });
}

describe('AlpacaActivityPageComponent', () => {
  it("renders Today's statement exactly as the data plane authored it", async () => {
    const { broker } = await renderActivity();

    await screen.findByText('Net today', { selector: 'dt' });
    expect(statementAmount('Realized gains and losses')).toBe('$12.34');
    expect(statementAmount('Fees')).toBe('$0.10');
    expect(statementAmount('Open gain or loss on shares held now')).toBe('-$3.00');
    expect(statementAmount('Net today')).toBe('$9.24');
    expect(broker.getPeriodFees).toHaveBeenCalledWith(TARGET, 'today');
    // Today shows the statement, never the curve.
    expect(broker.getPortfolioHistoryProof).not.toHaveBeenCalled();
  });

  it('never shows a missing amount as $0.00', async () => {
    const broker = brokers();
    broker.getPeriodFees.mockResolvedValue({
      ...periodFees('today'),
      statement: {
        state: 'unavailable', realized_usd: '12.34', fees_usd: '0.10', open_usd: null, net_usd: null,
        detail: 'Current prices are unavailable, so open gains and the net are not shown. Refresh to retry.',
      },
    });
    await renderActivity({ broker });

    await screen.findByText('Net today', { selector: 'dt' });
    expect(statementAmount('Open gain or loss on shares held now')).toBe('Unavailable');
    expect(statementAmount('Net today')).toBe('Unavailable');
    expect(screen.getByText(/Current prices are unavailable/)).toBeTruthy();
  });

  it("says why there is no statement or fee table when the account's fee record is offline, with a retry", async () => {
    const broker = brokers();
    broker.getPeriodFees.mockResolvedValue({
      account_id: null, observed_at_ms: OBSERVED_AT_MS, authority_revision: null, available: false, known: false,
      rows: [], account_unattributed_usd: null, period: 'today', period_start_ms: null, statement: null,
      messages: ['Fee evidence is unavailable while the account Clerk is offline.'],
    });
    await renderActivity({ broker });

    expect(await screen.findByText("Today's money is unavailable right now.")).toBeTruthy();
    expect(screen.getByText('Fee evidence is unavailable while the account Clerk is offline.')).toBeTruthy();
    expect(screen.queryByText('No fees were charged in this period.')).toBeNull();

    fireEvent.click(screen.getAllByRole('button', { name: 'Retry' })[0]);

    await waitFor(() => expect(broker.getPeriodFees).toHaveBeenCalledTimes(2));
  });

  it.each([
    ['Today', 'today', ['spy-ema-20260929-0930']],
    ['30D', '30d', ['spy-ema-20260929-0930', 'paper-ema-spy-0924', 'Outside order · MSFT']],
    ['60D', '60d', ['spy-ema-20260929-0930', 'paper-ema-spy-0924', 'paper-ema-spy-0801']],
  ] as const)('shows fees per bot for %s, each row named by the bot', async (label, id, owners) => {
    const { broker } = await renderActivity();

    fireEvent.click(await screen.findByRole('radio', { name: label }));

    await waitFor(() => expect(broker.getPeriodFees).toHaveBeenLastCalledWith(TARGET, id));
    await waitFor(() =>
      expect(within(feeTable()).getAllByRole('rowheader').map((cell) => cell.querySelector('.activity-table__owner')?.textContent))
        .toEqual(owners),
    );
    expect(broker.listActivities).toHaveBeenLastCalledWith(TARGET, { period: id, limit: 100 });
  });

  it('names an outside order by its order number, not a bare "External activity"', async () => {
    await renderActivity();

    fireEvent.click(await screen.findByRole('radio', { name: '30D' }));

    const row = await screen.findByRole('rowheader', { name: /Outside order · MSFT/ });
    expect(within(row).getByText('ord-7f3a')).toBeTruthy();
    expect(screen.queryByText('External activity')).toBeNull();
  });

  it('switches period from the keyboard as one named radio group', async () => {
    const { broker } = await renderActivity();
    const today = await screen.findByRole('radio', { name: 'Today' });
    expect(today.getAttribute('aria-checked')).toBe('true');
    expect(today.getAttribute('tabindex')).toBe('0');
    expect(period('30D').getAttribute('tabindex')).toBe('-1');

    fireEvent.keyDown(today, { key: 'ArrowRight' });

    expect(period('30D').getAttribute('aria-checked')).toBe('true');
    expect(document.activeElement).toBe(period('30D'));
    // 30D and 60D draw the account value curve with its equity check instead.
    await waitFor(() => expect(broker.getPortfolioHistoryProof).toHaveBeenLastCalledWith(TARGET, '30D'));
    expect(await screen.findByRole('heading', { name: '30D equity curve' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Portfolio equity check' })).toBeTruthy();
    expect(screen.queryByText('Net today', { selector: 'dt' })).toBeNull();

    fireEvent.keyDown(period('30D'), { key: 'End' });
    expect(period('60D').getAttribute('aria-checked')).toBe('true');
    fireEvent.keyDown(period('60D'), { key: 'ArrowRight' });
    expect(period('Today').getAttribute('aria-checked')).toBe('true');
    expect(document.activeElement).toBe(period('Today'));
  });

  it('shows the sync check at the top of the page', async () => {
    await renderActivity();

    const sync = await screen.findByText('Sync check');
    expect(sync.parentElement?.textContent).toContain('Clean');
    expect(sync.parentElement?.textContent).toContain('last checked');
  });

  it('keeps order records and recovery folded, unread, until opened', async () => {
    const { broker } = await renderActivity();

    const summary = await screen.findByText('Order records and recovery');
    const fold = summary.closest('details');
    expect(fold?.open).toBe(false);
    expect(broker.getSqliteClerkProjection).not.toHaveBeenCalled();
    expect(broker.getCustodyDiagnosis).not.toHaveBeenCalled();
    expect(broker.accountTransactions).not.toHaveBeenCalled();
  });

  it("opens the fold on a bot's recovery link and names no internal terms", async () => {
    const { broker } = await renderActivity({ query: { timelineBot: 'spy-ema-20260929-0930' } });

    const fold = (await screen.findByText('Order records and recovery')).closest('details');
    await waitFor(() => expect(fold?.open).toBe(true));
    await waitFor(() => expect(broker.getSqliteClerkProjection).toHaveBeenCalled());
    expect(await screen.findByText(/Order records are unavailable right now/)).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/SQLite|control boundary|data-plane|lens/i);
  });

  it('passes an AXE check', async () => {
    await renderActivity();
    await screen.findByText('Net today', { selector: 'dt' });
    await screen.findByRole('table', { name: 'Orders and cash moves' });

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });

    expect(results.violations).toEqual([]);
  });
});
