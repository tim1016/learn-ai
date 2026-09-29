import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { Router, provideRouter } from '@angular/router';
import { fireEvent, render, screen, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { provideFleetDirectory, testLane, TEST_ACCOUNT_ID, TEST_CLERK_ID } from '../../../fleet/fleet-directory-testing';
import { AlpacaLiveVerdictService } from '../../../services/alpaca-live-verdict.service';
import { formatReceiptLabel } from '../../../shared/pipes/receipt-label.pipe';
import { fakeVendorCatalog, provideFakeVendorCatalog } from '../../../shared/symbol-catalog/testing/fake-symbol-catalog';
import { fakeTickerCatalog, provideFakeTickerCatalog } from '../../../shared/ticker-catalog/testing/fake-ticker-catalog';
import { fakeVerdictState } from '../../../testing/alpaca-live-verdict-fixtures';
import { AlpacaHistoryPageComponent } from './alpaca-history-page.component';
import {
  BotHistoryService,
  type BotHistoryQuery,
  type FleetBotHistoryPage,
  type FleetBotHistoryRow,
} from './bot-history.service';

const LIVE_CLERK = 'clrk_live000000000000000000bb';
const LIVE_ACCOUNT = 'live-account-1';

function bot(overrides: Partial<FleetBotHistoryRow> = {}): FleetBotHistoryRow {
  return {
    broker: 'alpaca',
    clerk_id: TEST_CLERK_ID,
    strategy_instance_id: 'spy-ema-20260928-0931',
    strategy_key: 'ema_crossover',
    strategy_label: 'EMA crossover',
    symbol: 'SPY',
    account_id: TEST_ACCOUNT_ID.toUpperCase(),
    world: 'paper',
    world_label: 'PAPER · practice money',
    status: 'finished',
    status_label: 'Finished',
    started_at_ms: 1_790_000_000_000,
    stopped_at_ms: 1_790_000_600_000,
    outcome: {
      kind: 'STOPPED',
      reason_code: 'STOPPED_FLAT',
      headline: 'Stopped by you',
      recorded_at_ms: 1_790_000_600_000,
    },
    transaction_count: 4,
    orders: { sent: 3, filled: 2, cancelled: 1, rejected: 0 },
    budget_usd: '1500.00',
    result_usd: '12.34',
    fees_usd: '0.07',
    money_unavailable_reason: null,
    money_scope_note: null,
    runs: [],
    ...overrides,
  };
}

function page(overrides: Partial<FleetBotHistoryPage> = {}): FleetBotHistoryPage {
  return {
    observed_at_ms: 1_790_001_000_000,
    rows: [bot()],
    gaps: [],
    total: 1,
    page: 1,
    page_size: 25,
    symbols: ['QQQ', 'SPY'],
    ...overrides,
  };
}

async function renderHistory(answer: FleetBotHistoryPage, inputs: Record<string, string> = {}) {
  const read = vi.fn<(query: BotHistoryQuery) => Promise<FleetBotHistoryPage>>().mockResolvedValue(answer);
  const view = await render(AlpacaHistoryPageComponent, {
    inputs,
    providers: [
      provideRouter([]),
      provideHttpClient(),
      provideHttpClientTesting(),
      // The symbol filter is the shared instrument card over a host universe.
      provideFakeVendorCatalog(fakeVendorCatalog()),
      provideFakeTickerCatalog(fakeTickerCatalog()),
      provideFleetDirectory({
        observed_at_ms: 1,
        clerks: [
          testLane(),
          testLane({
            clerk_id: LIVE_CLERK,
            display_label: 'Live',
            provider_summary: { ...testLane().provider_summary, confirmed_account_id: LIVE_ACCOUNT },
          }),
        ],
      }),
      {
        provide: AlpacaLiveVerdictService,
        useValue: {
          stateFor: (clerkId: string) => fakeVerdictState(clerkId === LIVE_CLERK ? 'live' : 'paper'),
          start: vi.fn(),
        },
      },
      { provide: BotHistoryService, useValue: { read } },
    ],
  });
  await view.fixture.whenStable();
  view.fixture.detectChanges();
  return { ...view, read };
}

describe('AlpacaHistoryPageComponent', () => {
  it('lists every account with no filter pre-selected, one line per bot', async () => {
    const { read } = await renderHistory(page());

    expect(read).toHaveBeenCalledWith({ clerkId: null, status: null, world: null, symbol: null, page: 1 });
    expect((screen.getByLabelText('Account') as HTMLSelectElement).value).toBe('');
    const line = screen.getByRole('link', { name: 'spy-ema-20260928-0931' }).closest('tr') as HTMLElement;
    // The account is named with its mode worded, not by colour alone.
    expect(within(line).getByText('Paper')).toBeTruthy();
    expect(line.querySelector('app-alpaca-lane-mode-chip')?.textContent?.trim()).toBe('PAPER · practice money');
    expect(within(line).getByText('Stopped by you', { exact: false })).toBeTruthy();
    expect(within(line).getByText(formatReceiptLabel('STOPPED_FLAT'))).toBeTruthy();
    expect(within(line).getByText('Finished')).toBeTruthy();
    // Dollars are the backend's own strings, formatted, never recomputed.
    expect(within(line).getByText('$1,500.00')).toBeTruthy();
    expect(within(line).getByText('$12.34')).toBeTruthy();
    expect(within(line).getByText('$0.07')).toBeTruthy();
    expect(line.textContent).toContain('3 sent · 2 filled · 1 cancelled');
  });

  it("opens a bot's own page on its own account's workspace", async () => {
    await renderHistory(page({ rows: [bot({ clerk_id: LIVE_CLERK, account_id: 'LIVE-ACCOUNT-1', world: 'live' })] }));

    expect(screen.getByRole('link', { name: 'spy-ema-20260928-0931' }).getAttribute('href'))
      .toBe(`/brokers/alpaca/clerks/${LIVE_CLERK}/accounts/${LIVE_ACCOUNT}/bots/spy-ema-20260928-0931`);
  });

  it('shows an unknown result as unknown, with its reason, never as $0', async () => {
    await renderHistory(page({
      rows: [bot({ result_usd: null, fees_usd: null, money_unavailable_reason: 'Fee evidence is unresolved.' })],
    }));

    expect(screen.getAllByText('unknown')).toHaveLength(2);
    expect(screen.queryByText('$0.00')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Why unknown' }));

    expect(screen.getByText('Result and fees unknown: Fee evidence is unresolved.')).toBeTruthy();
  });

  it('expands an old bot with several runs to one line per run; its money stays on the bot', async () => {
    const run = (runId: string, transactions: number) => ({
      run_id: runId,
      started_at_ms: 1_789_000_000_000,
      stopped_at_ms: 1_789_000_100_000,
      running: false,
      outcome: null,
      transaction_count: transactions,
      orders: { sent: transactions, filled: transactions, cancelled: 0, rejected: 0 },
    });
    await renderHistory(page({
      rows: [bot({
        runs: [run('r2', 3), run('r1', 1)],
        money_scope_note: 'Result and fees are for all 2 runs of this bot; they are not split by run.',
      })],
    }));

    const toggle = screen.getByRole('button', { name: '2 runs' });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(toggle);

    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText('Run 2').closest('tr')?.textContent).toContain('3 sent · 3 filled');
    expect(screen.getByText('Run 1').closest('tr')?.textContent).toContain('1 sent · 1 filled');
    expect(screen.getByText('Result and fees are for all 2 runs of this bot; they are not split by run.')).toBeTruthy();
  });

  it('names every account it could not read, never leaving one silently out', async () => {
    await renderHistory(page({
      gaps: [
        {
          broker: 'alpaca', clerk_id: LIVE_CLERK, account_id: LIVE_ACCOUNT, strategy_instance_id: null,
          reason: "This account's bots could not be read right now. Refresh to try again.", reason_code: 'clerk_unreachable',
        },
        {
          broker: 'alpaca', clerk_id: TEST_CLERK_ID, account_id: TEST_ACCOUNT_ID, strategy_instance_id: 'spy-dry-1',
          reason: "This Dry Run's own records could not be read, so it is not listed.", reason_code: null,
        },
      ],
    }));

    const gaps = screen.getByRole('alert');
    expect(within(gaps).getByText('Live')).toBeTruthy();
    expect(gaps.textContent).toContain(formatReceiptLabel('clerk_unreachable'));
    expect(gaps.textContent).toContain('Dry Run spy-dry-1');
  });

  it('narrows the list from the filters, starting again from the first page', async () => {
    const { fixture } = await renderHistory(page(), { page: '3' });
    const router = fixture.debugElement.injector.get(Router);
    const navigate = vi.spyOn(router, 'navigate').mockResolvedValue(true);

    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'cleared' } });

    expect(navigate).toHaveBeenCalledWith([], expect.objectContaining({
      queryParams: { status: 'cleared', page: null },
      queryParamsHandling: 'merge',
    }));
  });

  it('reads again only when the owner asks: Refresh, never a poll', async () => {
    const { read, fixture } = await renderHistory(page());
    expect(read).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await fixture.whenStable();

    expect(read).toHaveBeenCalledTimes(2);
  });

  it('passes the accessibility checks', async () => {
    await renderHistory(page({ gaps: [] }));

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations.map((violation) => violation.id)).toEqual([]);
  });
});
