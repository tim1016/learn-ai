import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter, withComponentInputBinding } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
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
  botHistoryQuery,
  type BotHistoryQuery,
  type FleetBotHistoryPage,
  type FleetBotHistoryRow,
} from './bot-history.service';

const LIVE_CLERK = 'clrk_live000000000000000000bb';
const LIVE_ACCOUNT = 'live-account-1';

const ANY: BotHistoryQuery = { account: null, status: null, world: null, symbol: null, bot: null, page: 1 };

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
    page_unavailable_reason: null,
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

function historyProviders(read: (query: BotHistoryQuery) => Promise<FleetBotHistoryPage>) {
  return [
    provideHttpClient(),
    provideHttpClientTesting(),
    // The symbol filter is the shared symbol picker over a host universe.
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
  ];
}

async function renderHistory(answer: FleetBotHistoryPage, inputs: Record<string, string> = {}) {
  const read = vi.fn<(query: BotHistoryQuery) => Promise<FleetBotHistoryPage>>().mockResolvedValue(answer);
  const view = await render(AlpacaHistoryPageComponent, {
    inputs,
    providers: [provideRouter([]), ...historyProviders(read)],
  });
  await view.fixture.whenStable();
  view.fixture.detectChanges();
  const navigate = vi.spyOn(view.fixture.debugElement.injector.get(Router), 'navigate').mockResolvedValue(true);
  return { ...view, read, navigate };
}

describe('AlpacaHistoryPageComponent', () => {
  it('lists every account with no filter pre-selected, one line per bot', async () => {
    const { read } = await renderHistory(page());

    expect(read).toHaveBeenCalledWith(ANY);
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

  it("wears the world each bot's run was in, never its lane's mode today (#2615)", async () => {
    // Both rows sit on the Live lane: a Dry Run and an old Shadow rehearsal
    // never wear its red "real money" chip.
    await renderHistory(page({
      rows: [
        bot({ strategy_instance_id: 'dry-1', clerk_id: LIVE_CLERK, world: 'dry_run', world_label: 'DRY RUN · simulated cash' }),
        bot({
          strategy_instance_id: 'rehearsal', clerk_id: LIVE_CLERK, world: 'shadow',
          world_label: 'SHADOW · simulated fills on your live account',
        }),
        bot({ strategy_instance_id: 'real', clerk_id: LIVE_CLERK, world: 'live', world_label: 'LIVE · real money' }),
      ],
    }));

    const chip = (sid: string) =>
      (screen.getByRole('link', { name: sid }).closest('tr') as HTMLElement).querySelector('.lane-mode-chip') as HTMLElement;
    expect([chip('dry-1'), chip('rehearsal'), chip('real')].map((element) => element.textContent?.trim())).toEqual([
      'DRY RUN · simulated cash', 'SHADOW · simulated fills on your live account', 'LIVE · real money',
    ]);
    expect(chip('dry-1').classList).toContain('lane-mode-chip--dry_run');
    expect(chip('rehearsal').classList).toContain('lane-mode-chip--shadow');
    expect(chip('real').classList).toContain('lane-mode-chip--live');
  });

  it("names a bot whose page cannot open, with why, and offers no dead link (#2614)", async () => {
    const why = "This bot ran in the account's Shadow world, which the account's pages don't open, "
      + 'so it has no page of its own. History keeps its record.';
    await renderHistory(page({
      rows: [bot({ strategy_instance_id: 'sh-dv-spy-0916', clerk_id: LIVE_CLERK, world: 'shadow', page_unavailable_reason: why })],
    }));

    expect(screen.queryByRole('link', { name: 'sh-dv-spy-0916' })).toBeNull();
    const line = screen.getByText('sh-dv-spy-0916').closest('tr') as HTMLElement;
    expect(within(line).getByText(why)).toBeTruthy();
  });

  it('names each account in the filter with its mode worded', async () => {
    await renderHistory(page());

    const options = within(screen.getByLabelText('Account')).getAllByRole('option').map((option) => option.textContent?.trim());
    expect(options).toEqual(['All accounts', 'Paper · PAPER · practice money', 'Live · LIVE · real money']);
  });

  it("opens a bot's own page on its own account's workspace, from its one link", async () => {
    await renderHistory(page({ rows: [bot({ clerk_id: LIVE_CLERK, account_id: 'LIVE-ACCOUNT-1', world: 'live' })] }));

    const link = screen.getByRole('link', { name: 'spy-ema-20260928-0931' });
    expect(link.getAttribute('href'))
      .toBe(`/brokers/alpaca/clerks/${LIVE_CLERK}/accounts/${LIVE_ACCOUNT}/bots/spy-ema-20260928-0931`);
    // The whole line opens it: that one link, stretched over the row.
    expect(within(link.closest('tr') as HTMLElement).getAllByRole('link')).toEqual([link]);
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

  it('names every account and bot it could not read, never by a raw lane id', async () => {
    await renderHistory(page({
      gaps: [
        {
          broker: 'alpaca', clerk_id: LIVE_CLERK, account_id: LIVE_ACCOUNT, strategy_instance_id: null,
          reason: "Account 'live-account-1' is not this Clerk's account.", reason_code: 'lane_refused_read',
        },
        {
          broker: 'alpaca', clerk_id: TEST_CLERK_ID, account_id: TEST_ACCOUNT_ID, strategy_instance_id: 'spy-dry-1',
          reason: "This Dry Run's own records could not be read, so it is not listed.", reason_code: null,
        },
        {
          broker: 'alpaca', clerk_id: 'clrk_retired00000000000000cc', account_id: null, strategy_instance_id: null,
          reason: 'This account is not set up yet, so its bots cannot be listed.', reason_code: 'no_confirmed_account',
        },
      ],
    }));

    const gaps = screen.getByRole('alert');
    expect(within(gaps).getByText('Live')).toBeTruthy();
    expect(gaps.textContent).toContain(formatReceiptLabel('lane_refused_read'));
    expect(gaps.textContent).toContain("Account 'live-account-1' is not this Clerk's account.");
    expect(within(gaps).getByText('spy-dry-1')).toBeTruthy();
    expect(within(gaps).getByText('Unnamed account')).toBeTruthy();
    expect(gaps.textContent).not.toContain('clrk_');
  });

  it('narrows the list from the filters, starting again from the first page', async () => {
    const { navigate } = await renderHistory(page(), { page: '3' });

    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'cleared' } });

    expect(navigate).toHaveBeenCalledWith([], expect.objectContaining({
      queryParams: { status: 'cleared', page: null },
      queryParamsHandling: 'merge',
    }));
  });

  it("opens with the URL's filters selected and read — where Home's Finished link lands", async () => {
    const { read } = await renderHistory(page({ rows: [bot({ status: 'cleared', status_label: 'Cleared' })] }), {
      status: 'cleared',
      world: 'paper',
      page: '2',
    });

    expect(read).toHaveBeenCalledWith({ ...ANY, status: 'cleared', world: 'paper', page: 2 });
    expect((screen.getByLabelText('Status') as HTMLSelectElement).value).toBe('cleared');
    expect((screen.getByLabelText('World') as HTMLSelectElement).value).toBe('paper');
  });

  it('treats what the list does not know in the URL as unset, never as an unreadable list', async () => {
    const { read } = await renderHistory(page(), { account: 'clrk_gone', symbol: 'not a symbol!', status: 'lost' });

    expect(read).toHaveBeenCalledWith(ANY);
    expect(screen.queryByText('History could not be read. Refresh to try again.')).toBeNull();
  });

  it("opens on one bot from its own page, and widens back to every bot", async () => {
    const { read, navigate } = await renderHistory(page(), { account: TEST_CLERK_ID, bot: 'spy-ema-20260928-0931' });

    expect(read).toHaveBeenCalledWith({ ...ANY, account: TEST_CLERK_ID, bot: 'spy-ema-20260928-0931' });
    expect((screen.getByLabelText('Account') as HTMLSelectElement).value).toBe(TEST_CLERK_ID);
    expect(screen.getByText('One bot:', { exact: false }).textContent).toContain('spy-ema-20260928-0931');
    fireEvent.click(screen.getByRole('button', { name: 'Every bot' }));

    expect(navigate).toHaveBeenCalledWith([], expect.objectContaining({ queryParams: { bot: null, page: null } }));
  });

  it("narrows to a symbol from the history's own symbols, ungated", async () => {
    const { navigate } = await renderHistory(page());

    fireEvent.click(screen.getByRole('combobox', { name: 'Symbol' }));
    const options = screen.getAllByRole('option');
    expect(options.map((option) => option.textContent ?? '').join(' ')).toContain('QQQ');
    fireEvent.click(options.find((option) => option.textContent?.includes('QQQ')) as HTMLElement);

    expect(navigate).toHaveBeenCalledWith([], expect.objectContaining({ queryParams: { symbol: 'QQQ', page: null } }));
  });

  it('clears the symbol filter with Any symbol', async () => {
    const { read, navigate } = await renderHistory(page(), { symbol: 'qqq' });

    expect(read).toHaveBeenCalledWith({ ...ANY, symbol: 'QQQ' });
    fireEvent.click(screen.getByRole('button', { name: 'Any symbol' }));

    expect(navigate).toHaveBeenCalledWith([], expect.objectContaining({ queryParams: { symbol: null, page: null } }));
  });

  it('pages Newer and Older, and moves focus to the page count after paging', async () => {
    const { read, navigate, fixture } = await renderHistory(page({ page: 2, total: 60 }), { page: '2' });

    expect(read).toHaveBeenCalledWith({ ...ANY, page: 2 });
    const count = screen.getByText('Page 2 of 3 · 60 bots');
    fireEvent.click(screen.getByRole('button', { name: 'Older' }));
    fixture.detectChanges();
    await fixture.whenStable();

    expect(navigate).toHaveBeenLastCalledWith([], expect.objectContaining({ queryParams: { page: '3' } }));
    expect(document.activeElement).toBe(count);
    fireEvent.click(screen.getByRole('button', { name: 'Newer' }));
    expect(navigate).toHaveBeenLastCalledWith([], expect.objectContaining({ queryParams: { page: null } }));
  });

  it('keeps the last page on screen, marked busy, while the next is read', async () => {
    const { read, fixture } = await renderHistory(page({ page: 1, total: 60 }));
    read.mockReturnValueOnce(new Promise(() => undefined));

    fixture.componentRef.setInput('page', '2');
    fixture.detectChanges();
    // The read never answers, so the page is not stable: let the loader start.
    await new Promise((resolve) => setTimeout(resolve, 0));
    fixture.detectChanges();

    expect(read).toHaveBeenLastCalledWith({ ...ANY, page: 2 });
    expect(screen.getByRole('link', { name: 'spy-ema-20260928-0931' })).toBeTruthy();
    expect(screen.getByRole('navigation', { name: 'History pages' })).toBeTruthy();
    expect(screen.getByRole('table').closest('[aria-busy]')?.getAttribute('aria-busy')).toBe('true');
    // Refresh never disables itself under the owner's focus.
    expect((screen.getByRole('button', { name: 'Refresh' }) as HTMLButtonElement).disabled).toBe(false);
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

  it("binds the URL's query to the page through the router (withComponentInputBinding)", async () => {
    const read = vi.fn<(query: BotHistoryQuery) => Promise<FleetBotHistoryPage>>().mockResolvedValue(page());
    TestBed.configureTestingModule({
      providers: [
        provideRouter(
          [{ path: 'brokers/:broker/clerks/:clerkId/history', component: AlpacaHistoryPageComponent }],
          withComponentInputBinding(),
        ),
        ...historyProviders(read),
      ],
    });
    const harness = await RouterTestingHarness.create();

    await harness.navigateByUrl(`/brokers/alpaca/clerks/${TEST_CLERK_ID}/history?status=cleared&page=2`);

    expect(read).toHaveBeenCalledWith({ ...ANY, status: 'cleared', page: 2 });
  });
});

describe('botHistoryQuery', () => {
  const known = (clerkId: string) => clerkId === TEST_CLERK_ID;

  it('reads every filter the URL carries', () => {
    expect(botHistoryQuery(
      { account: TEST_CLERK_ID, status: 'holding', world: 'dry_run', symbol: ' brk.b ', bot: 'spy-1', page: '4' },
      known,
    )).toEqual({ account: TEST_CLERK_ID, status: 'holding', world: 'dry_run', symbol: 'BRK.B', bot: 'spy-1', page: 4 });
  });

  it.each([
    ['an account no lane serves', { account: 'clrk_gone' }],
    ['a malformed symbol', { symbol: 'SPY 500' }],
    ['an unknown status', { status: 'paused' }],
    ['an unknown world', { world: 'moon' }],
    ['a bot id longer than the coordinator accepts', { bot: 'x'.repeat(129) }],
    ['a page that is not a positive whole number', { page: '-2' }],
  ])('treats %s as unset', (_case, values) => {
    expect(botHistoryQuery(values, known)).toEqual(ANY);
  });
});
