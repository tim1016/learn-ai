import { fireEvent, render, screen, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type {
  BrokerAccountSnapshot,
  BrokerActivity,
  BrokerPortfolioHistory,
  BrokerPosition,
  PortfolioHistoryProof,
} from '../../../api/alpaca.types';
import { BrokerService } from '../../../services/broker.service';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaDeskAccountDataService } from './alpaca-desk-account-data.service';
import { AlpacaTraderLensComponent } from './alpaca-trader-lens.component';

vi.mock('lightweight-charts', () => {
  const chart = {
    addSeries: vi.fn().mockReturnValue({ setData: vi.fn() }),
    applyOptions: vi.fn(),
    remove: vi.fn(),
    timeScale: vi.fn().mockReturnValue({ fitContent: vi.fn() }),
  };
  return {
    createChart: vi.fn().mockReturnValue(chart),
    LineSeries: 'LineSeries',
    TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
  };
});

function account(): BrokerAccountSnapshot {
  return {
    broker: 'alpaca',
    account_id: 'PA1',
    account_mode: 'paper',
    account_status: 'ACTIVE',
    currency: 'USD',
    cash: 1_500,
    equity: 10_250,
    buying_power: 20_500,
    portfolio_value: 10_250,
    long_market_value: 8_750,
    short_market_value: 0,
    pattern_day_trader: false,
    trading_blocked: false,
    account_blocked: false,
    created_at_ms: null,
    observed_at_ms: 1_700_000_000_000,
  };
}

function positions(): BrokerPosition[] {
  return [
    {
      broker: 'alpaca',
      symbol: 'SPY',
      asset_id: 'spy',
      asset_class: 'us_equity',
      quantity: 10,
      side: 'long',
      average_entry_price: 500,
      market_value: 5_100,
      cost_basis: 5_000,
      current_price: 510,
      unrealized_pl: 100,
      unrealized_plpc: 0.02,
      observed_at_ms: 1_700_000_000_000,
    },
    {
      broker: 'alpaca',
      symbol: 'NVDA',
      asset_id: 'nvda',
      asset_class: 'us_equity',
      quantity: 5,
      side: 'long',
      average_entry_price: 800,
      market_value: 4_000,
      cost_basis: 4_000,
      current_price: 800,
      unrealized_pl: 0,
      unrealized_plpc: 0,
      observed_at_ms: 1_700_000_000_000,
    },
  ];
}

function activities(): BrokerActivity[] {
  return [
    {
      broker: 'alpaca',
      activity_id: 'fill-newer',
      native_order_id: 'order-2',
      activity_type: 'FILL',
      category: 'trade_activity',
      symbol: 'NVDA',
      side: 'buy',
      quantity: 5,
      price: 800,
      net_amount: -4_000,
      occurred_at_ms: 1_700_000_000_200,
      observed_at_ms: 1_700_000_000_200,
    },
    {
      broker: 'alpaca',
      activity_id: 'fill-older',
      native_order_id: 'order-1',
      activity_type: 'FILL',
      category: 'trade_activity',
      symbol: 'SPY',
      side: 'sell',
      quantity: 10,
      price: 510,
      net_amount: 5_100,
      occurred_at_ms: 1_700_000_000_100,
      observed_at_ms: 1_700_000_000_100,
    },
  ];
}

function portfolioHistory(): BrokerPortfolioHistory {
  return {
    timestamps: [1_700_000_000_000, 1_700_086_400_000],
    equity: [10_000, 10_125],
    profit_loss: [0, 125],
    base_value: 10_000,
    timeframe: '1D',
  };
}

function portfolioHistoryProof(): PortfolioHistoryProof {
  return {
    history: portfolioHistory(),
    attribution: {
      account_id: 'PA1',
      authority_generation: 1,
      control_revision: 2,
      from_ms: 1_700_000_000_000,
      to_ms: 1_700_086_400_000,
      attribution_rows: [{
        symbol: 'SPY',
        quantity: 10,
        entry_price: 500,
        exit_price: 510,
        opened_at_ms: 1_700_000_000_000,
        closed_at_ms: 1_700_086_400_000,
        realized_pnl: 100,
        fee: null,
        entry_strategy_instance_id: 'bot-spy',
        exit_strategy_instance_id: 'bot-spy',
        entry_subject_id: 'bot:bot-spy',
        exit_subject_id: 'bot:bot-spy',
      }],
      realized_pnl_total: 100,
      start_open_pnl_total: 0,
      open_pnl_total: 0,
      fee_total: 0,
      fee_fidelity: 'reported',
      execution_coverage: 'complete',
      marks_complete: true,
      start_mark_observed_at_ms: {},
      mark_observed_at_ms: {},
    },
    reconciliation: {
      broker_delta: 125,
      local_delta: 125,
      residual: 0,
      within_tolerance: true,
      atol: 0.000001,
      rtol: 0,
      divergences: [],
    },
  };
}

function transactionHistory() {
  return {
    projection_available: true,
    canonical_fallback_required: false,
    feed_state: 'live' as const,
    feed_headline: 'Transaction history current.',
    feed_detail: 'The Clerk projection is current.',
    high_water_journal_seq: 1,
    lag_records: 0,
    lag_is_lower_bound: false,
    custody_summary: {
      record_count: 0,
      a0_custody_accepted_count: 0,
      a1_broker_write_started_count: 0,
      a2_broker_known_count: 0,
      a3_economic_terminal_count: 0,
      uncertain_count: 0,
    },
    rows: [],
    next_cursor: null,
  };
}

function brokers() {
  return {
    getAccount: vi.fn().mockResolvedValue(account()),
    listPositions: vi.fn().mockResolvedValue(positions()),
    listActivities: vi.fn().mockResolvedValue(activities()),
    getPortfolioHistoryProof: vi.fn().mockResolvedValue(portfolioHistoryProof()),
  };
}

async function renderLens(
  broker = brokers(),
  clerk = { accountTransactions: vi.fn().mockResolvedValue(transactionHistory()) },
) {
  await render(AlpacaTraderLensComponent, {
    providers: [
      AlpacaDeskAccountDataService,
      { provide: BrokersService, useValue: broker },
      { provide: BrokerService, useValue: clerk },
    ],
  });
  return { broker, clerk };
}

describe('AlpacaTraderLensComponent', () => {
  it("renders today's live positions, fill count, and instrument identities", async () => {
    const { broker } = await renderLens();

    expect(await screen.findAllByTitle('SPY')).not.toHaveLength(0);
    const hero = screen.getByLabelText('Today at a glance');
    expect(within(hero).getAllByText('2')).toHaveLength(2);
    expect(screen.getByText('Fills today')).toBeTruthy();
    expect(within(hero).getByText('Open positions')).toBeTruthy();
    expect(within(hero).getByText('Realized P&L today')).toBeTruthy();
    expect(within(hero).getByText('Reconciled account attribution is not available yet.')).toBeTruthy();
    expect(screen.getByRole('list', { name: 'Today at the desk activity' })).toBeTruthy();
    expect(screen.getAllByTitle('NVDA')).not.toHaveLength(0);
    expect(broker.listActivities).toHaveBeenCalledWith(
      'alpaca',
      expect.objectContaining({ currentSession: true, limit: 100 }),
    );
  });

  it('renders the broker curve and paged Clerk history for 30D and 60D scopes', async () => {
    const { broker, clerk } = await renderLens();

    fireEvent.click(screen.getByRole('button', { name: '30D' }));

    expect(screen.getByRole('button', { name: '30D' }).getAttribute('aria-pressed')).toBe('true');
    expect(await screen.findByRole('heading', { name: '30D equity curve' })).toBeTruthy();
    expect(await screen.findByRole('img', { name: '30D broker equity curve' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Transaction history' })).toBeTruthy();
    expect(broker.getPortfolioHistoryProof).toHaveBeenCalledWith('alpaca', '30D');
    expect(await screen.findByText(/Broker curve agrees with local FIFO P&L within \$0\.000001\./)).toBeTruthy();
    expect(screen.getByRole('table', { name: 'FIFO attribution rows' })).toBeTruthy();
    expect(screen.getAllByTitle('SPY')).not.toHaveLength(0);
    await vi.waitFor(() => expect(clerk.accountTransactions).toHaveBeenCalled());
    const thirtyDayFilters = clerk.accountTransactions.mock.calls.at(-1)?.[3];
    expect(thirtyDayFilters).toMatchObject({
      fromMs: 1_700_000_000_000,
      toMs: 1_700_086_400_000,
    });
    expect(screen.queryByRole('heading', { name: 'Today at the desk' })).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '60D' }));

    expect(await screen.findByRole('heading', { name: '60D equity curve' })).toBeTruthy();
    expect(await screen.findByRole('img', { name: '60D broker equity curve' })).toBeTruthy();
    expect(broker.getPortfolioHistoryProof).toHaveBeenLastCalledWith('alpaca', '60D');
    await vi.waitFor(() => {
      const sixtyDayFilters = clerk.accountTransactions.mock.calls.at(-1)?.[3];
      expect(sixtyDayFilters).toMatchObject({
        fromMs: 1_700_000_000_000,
        toMs: 1_700_086_400_000,
      });
    });
  });

  it('renders zero fills as a loaded value', async () => {
    const broker = brokers();
    broker.listActivities.mockResolvedValue([]);
    await renderLens(broker);

    const hero = await screen.findByLabelText('Today at a glance');
    const fills = within(hero).getByText('Fills today').closest('article');
    expect(fills?.textContent).toContain('0');
    expect(fills?.textContent).not.toContain('Loading');
  });

  it('renders backend-authored reconciliation divergences', async () => {
    const broker = brokers();
    broker.getPortfolioHistoryProof.mockResolvedValue({
      ...portfolioHistoryProof(),
      reconciliation: {
        broker_delta: 125,
        local_delta: 100,
        residual: 25,
        within_tolerance: false,
        atol: 0.000001,
        rtol: 0,
        divergences: [{
          category: 'pnl_drift',
          detail: 'The complete broker and FIFO books differ by $25.',
        }],
      },
    });
    await renderLens(broker);

    fireEvent.click(screen.getByRole('button', { name: '30D' }));

    expect(await screen.findByText('P&L Drift')).toBeTruthy();
    expect(screen.getByText('The complete broker and FIFO books differ by $25.')).toBeTruthy();
  });

  it('distinguishes an unavailable activity feed from an empty day', async () => {
    const broker = brokers();
    broker.listActivities.mockRejectedValue(new Error('unreachable'));
    await renderLens(broker);

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain("Today's account activity is unavailable");
    expect(screen.queryByText('No account activity has been recorded today.')).toBeNull();
  });

  it('keeps the broker curve visible when the independent proof is unavailable', async () => {
    const broker = brokers();
    broker.getPortfolioHistoryProof.mockResolvedValue({
      history: portfolioHistory(),
      attribution: null,
      reconciliation: null,
      proof_unavailable_reason: 'SQLite FIFO attribution is unavailable for this broker.',
    });
    await renderLens(broker);

    fireEvent.click(screen.getByRole('button', { name: '30D' }));

    expect(await screen.findByRole('img', { name: '30D broker equity curve' })).toBeTruthy();
    expect(await screen.findByText('SQLite FIFO attribution is unavailable for this broker.')).toBeTruthy();
  });
});
