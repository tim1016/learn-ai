import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import axe from 'axe-core';
import { of } from 'rxjs';
import { describe, expect, it, vi } from 'vitest';

import type {
  ActivityPeriod,
  ActivityPeriodRead,
  BrokerActivity,
  PortfolioHistoryProof,
  TodayStatement,
} from '../../../../api/alpaca.types';
import type { components } from '../../../../api/broker.types';
import { resourceTarget } from '../../../../fleet/resource-target';
import { BrokersService } from '../../../../services/brokers.service';
import { AlpacaDeskAccountDataService } from '../alpaca-desk-account-data.service';
import { ALPACA_PORTFOLIO_HISTORY_CHART_FACTORY } from '../alpaca-portfolio-history-chart.component';
import { AlpacaActivityPageComponent } from './alpaca-activity-page.component';

type FeeView = components['schemas']['DeploymentFeeAttribution'];
type FeeRow = components['schemas']['DeploymentFeeRow'];

const TARGET = resourceTarget('alpaca', 'clrk_spec', { accountId: 'PA1', bindingGeneration: 1, routingEpoch: 1 });
const FENCE = { bindingGeneration: 1, routingEpoch: 1 };
const OBSERVED_AT_MS = 1_790_000_000_000;
const LAST_CLOSE_MS = OBSERVED_AT_MS - 20 * 3_600_000;

function botRow(sid: string, total: string): FeeRow {
  return {
    subject_id: `bot:${sid}`, strategy_instance_id: sid, label: sid, order_id: null,
    estimated_usd: total, modelled_settled_usd: '0', observed_usd: '0', total_usd: total,
  };
}

const OUTSIDE_ROW: FeeRow = {
  subject_id: 'external:ord-7f3a', strategy_instance_id: null, label: 'Outside order · MSFT', order_id: 'ord-7f3a',
  estimated_usd: '0', modelled_settled_usd: '0', observed_usd: '0.05', total_usd: '0.05',
};

/** Python-authored strings, exactly as the data plane sends them. */
const TODAY: TodayStatement = {
  state: 'ready', detail: null, since_ms: LAST_CLOSE_MS, observed_at_ms: OBSERVED_AT_MS,
  realized_usd: '12.34', fees_usd: '0.10', open_change_usd: '-3.00', net_usd: '9.24',
};

/** Each period's own fee rows: the bots that paid fees in that period only. */
const PERIOD_ROWS: Readonly<Record<ActivityPeriod, readonly FeeRow[]>> = {
  today: [botRow('spy-ema-20260929-0930', '0.10')],
  '30d': [botRow('spy-ema-20260929-0930', '0.10'), botRow('paper-ema-spy-0924', '0.44'), OUTSIDE_ROW],
  '60d': [botRow('spy-ema-20260929-0930', '0.10'), botRow('paper-ema-spy-0924', '0.44'), botRow('paper-ema-spy-0801', '0.31')],
};

function periodFees(period: ActivityPeriod): FeeView {
  return {
    account_id: 'PA1', observed_at_ms: OBSERVED_AT_MS, authority_revision: 3, available: true, known: true,
    account_unattributed_usd: '0', messages: [], rows: [...PERIOD_ROWS[period]],
    period, period_start_ms: OBSERVED_AT_MS - 86_400_000,
  };
}

function fill(id = 'fill-1'): BrokerActivity {
  return {
    broker: 'alpaca', activity_id: id, native_order_id: 'order-1', activity_type: 'FILL',
    category: 'trade_activity', symbol: 'SPY', side: 'buy', quantity: 12, price: 668.12, net_amount: -8_017.44,
    occurred_at_ms: OBSERVED_AT_MS - 60_000, observed_at_ms: OBSERVED_AT_MS - 60_000,
  };
}

function periodRead(
  period: ActivityPeriod,
  activities: BrokerActivity[] = [fill()],
  evidence: { complete?: boolean; token?: string | null } = {},
): ActivityPeriodRead {
  return {
    period, period_start_ms: OBSERVED_AT_MS - 86_400_000, observed_at_ms: OBSERVED_AT_MS,
    evidence: { activities, history_complete: evidence.complete ?? true, next_page_token: evidence.token ?? null },
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
    getFeeAttribution: vi.fn((_target: unknown, _sid: string | null, period: ActivityPeriod) =>
      Promise.resolve(periodFees(period))),
    getTodayStatement: vi.fn().mockResolvedValue(TODAY),
    getActivityPeriod: vi.fn((_target: unknown, period: ActivityPeriod) => Promise.resolve(periodRead(period))),
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

/** Who each fee block on the page belongs to, in order. */
function feeOwners(): string[] {
  const fees = screen.getByRole('region', { name: 'Fees' });
  return Array.from(fees.querySelectorAll('.fee-row > strong')).map((owner) => owner.textContent?.trim() ?? '');
}

function period(name: string): HTMLElement {
  return within(screen.getByRole('radiogroup', { name: 'Activity period' })).getByRole('radio', { name });
}

describe('AlpacaActivityPageComponent', () => {
  it("renders Today's statement exactly as the data plane authored it, since the last close", async () => {
    const { broker } = await renderActivity();

    await screen.findByText('Net today', { selector: 'dt' });
    expect(statementAmount('Realized gains and losses')).toBe('$12.34');
    expect(statementAmount('Fees billed today')).toBe('$0.10');
    expect(statementAmount('Change in open gains')).toBe('-$3.00');
    expect(statementAmount('Net today')).toBe('$9.24');
    expect(screen.getByText(/since the last close/)).toBeTruthy();
    expect(broker.getTodayStatement).toHaveBeenCalledWith(TARGET);
    // Today shows the statement, never the curve.
    expect(broker.getPortfolioHistoryProof).not.toHaveBeenCalled();
  });

  it("says in its heading that the statement covers the bots and this app's orders, not the account", async () => {
    await renderActivity();

    expect(await screen.findByRole('heading', { name: "Today · bots and this app's orders", level: 2 })).toBeTruthy();
    expect(screen.getByText(/not the whole account's day/)).toBeTruthy();
  });

  it('never shows a missing amount as $0.00', async () => {
    const broker = brokers();
    broker.getTodayStatement.mockResolvedValue({
      ...TODAY,
      state: 'unavailable', open_change_usd: null, net_usd: null,
      detail: 'A share held at the last close has no closing price here, so the change in open gains and the net are not shown.',
    });
    await renderActivity({ broker });

    await screen.findByText('Net today', { selector: 'dt' });
    expect(statementAmount('Change in open gains')).toBe('Unavailable');
    expect(statementAmount('Net today')).toBe('Unavailable');
    expect(screen.getByText(/has no closing price here/)).toBeTruthy();
  });

  it("shows the data plane's reason when the account Clerk is offline, with a retry", async () => {
    const broker = brokers();
    broker.getTodayStatement.mockResolvedValue({
      state: 'unavailable', since_ms: null, observed_at_ms: OBSERVED_AT_MS,
      realized_usd: null, fees_usd: null, open_change_usd: null, net_usd: null,
      detail: "Today's figures are unavailable while the account Clerk is offline.",
    });
    await renderActivity({ broker });

    const today = await screen.findByRole('region', { name: "Today · bots and this app's orders" });
    expect(await within(today).findByText("Today's figures are unavailable while the account Clerk is offline.")).toBeTruthy();
    expect(within(today).queryByText('Net today')).toBeNull();

    fireEvent.click(within(today).getByRole('button', { name: 'Retry' }));

    await waitFor(() => expect(broker.getTodayStatement).toHaveBeenCalledTimes(2));
  });

  it.each([
    ['Today', 'today', ['spy-ema-20260929-0930']],
    ['30D', '30d', ['spy-ema-20260929-0930', 'paper-ema-spy-0924', 'Outside order · MSFT']],
    ['60D', '60d', ['spy-ema-20260929-0930', 'paper-ema-spy-0924', 'paper-ema-spy-0801']],
  ] as const)('shows fees per bot for %s, each named by the bot', async (label, id, owners) => {
    const { broker } = await renderActivity();

    fireEvent.click(await screen.findByRole('radio', { name: label }));

    await waitFor(() => expect(broker.getFeeAttribution).toHaveBeenLastCalledWith(TARGET, null, id));
    await waitFor(() => expect(feeOwners()).toEqual(owners));
    expect(broker.getActivityPeriod).toHaveBeenLastCalledWith(TARGET, id);
  });

  it('reads Today\'s statement only on Today', async () => {
    const { broker } = await renderActivity();
    await screen.findByText('Net today', { selector: 'dt' });

    fireEvent.click(screen.getByRole('radio', { name: '30D' }));
    fireEvent.click(screen.getByRole('radio', { name: '60D' }));

    await waitFor(() => expect(broker.getPortfolioHistoryProof).toHaveBeenLastCalledWith(TARGET, '60D'));
    expect(broker.getTodayStatement).toHaveBeenCalledTimes(1);
  });

  it('names an outside order by its order number, not a bare "External activity"', async () => {
    await renderActivity();

    fireEvent.click(await screen.findByRole('radio', { name: '30D' }));

    expect(await screen.findByText('ord-7f3a')).toBeTruthy();
    expect(screen.getByText('Outside order · MSFT')).toBeTruthy();
    expect(screen.queryByText('External activity')).toBeNull();
  });

  it('says when a period holds older orders and cash moves than it shows, and loads them on request', async () => {
    const broker = brokers();
    broker.getActivityPeriod.mockImplementation((_target: unknown, period: ActivityPeriod, token?: string | null) =>
      Promise.resolve(
        token === 'tok-older'
          ? periodRead(period, [fill('fill-3')])
          : periodRead(period, [fill('fill-1'), fill('fill-2')], { complete: false, token: 'tok-older' }),
      ));
    await renderActivity({ broker });

    expect(await screen.findByText(/Showing the newest 2 orders and cash moves/)).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Load older' }));

    await waitFor(() => expect(broker.getActivityPeriod).toHaveBeenLastCalledWith(TARGET, 'today', 'tok-older'));
    const count = await screen.findByText('3 orders and cash moves in this period.');
    expect(screen.queryByRole('button', { name: 'Load older' })).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(count));
  });

  it('keeps "Load older" and says so when an older read fails', async () => {
    const broker = brokers();
    broker.getActivityPeriod.mockImplementation((_target: unknown, period: ActivityPeriod, token?: string | null) =>
      token === 'tok-older'
        ? Promise.reject(new Error('rate limited'))
        : Promise.resolve(periodRead(period, [fill('fill-1'), fill('fill-2')], { complete: false, token: 'tok-older' })));
    await renderActivity({ broker });

    fireEvent.click(await screen.findByRole('button', { name: 'Load older' }));

    expect(await screen.findByText('Older orders and cash moves could not be read. Try again.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Load older' })).toBeTruthy();
  });

  it('says so when Alpaca stops listing before the period starts', async () => {
    const broker = brokers();
    broker.getActivityPeriod.mockResolvedValue(periodRead('today', [fill('fill-1'), fill('fill-2')], { complete: false }));
    await renderActivity({ broker });

    expect(await screen.findByText(/Alpaca stopped listing before the start of this period/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Load older' })).toBeNull();
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

  it('gives every section on the page the same heading level', async () => {
    await renderActivity();
    await screen.findByText('Net today', { selector: 'dt' });
    await screen.findByRole('heading', { name: 'Orders and cash moves' });

    expect(screen.getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent?.trim())).toEqual([
      'Activity',
      "Today · bots and this app's orders",
      'Fees',
      'Orders and cash moves',
      'Order records and recovery',
    ]);
    expect(screen.queryAllByRole('heading', { level: 3 })).toEqual([]);
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
