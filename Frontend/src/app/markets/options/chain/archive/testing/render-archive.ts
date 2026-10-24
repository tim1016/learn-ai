import { fireEvent, render, screen } from '@testing-library/angular';
import type { Mock, vi as Vi } from 'vitest';

import { OptionsBacktestService } from '../../../../../components/options-backtest/options-backtest.service';
import type { SetupLimits } from '../../../../../components/options-backtest/options-backtest.types';
import { SERVED_LIMITS } from '../../../../../components/options-backtest/testing/fixtures';
import { reads } from '../../../../../components/options-backtest/testing/page-actions';
import { fakePickerWorld } from '../../../../../shared/symbol-picker/testing/fake-picker-world';
import { PastChainService, type DayQuery, type ExpiryQuery } from '../past-chain.service';
import type { ChainDay, ChainExpiry, DownloadAnswer, DownloadRequest } from '../past-chain.types';
import { ArchiveHostComponent } from './archive-host.component';
import { chainDay, chainExpiry, downloadAnswer, EXPIRY_MS, TRADING_DATE } from './fixtures';

const MONTH_NAMES: Readonly<Record<string, string>> = { '01': 'Jan', '02': 'Feb', '03': 'Mar', '04': 'Apr', '05': 'May', '06': 'Jun', '07': 'Jul', '08': 'Aug', '09': 'Sep', '10': 'Oct', '11': 'Nov', '12': 'Dec' };

/** The `vi` of the spec that calls: a shared helper never imports vitest itself. */
type Spy = Pick<typeof Vi, 'fn'>;

export interface FakeChainService {
  day: Mock<(query: DayQuery) => Promise<ChainDay>>;
  expiry: Mock<(query: ExpiryQuery) => Promise<ChainExpiry>>;
  download: Mock<(request: DownloadRequest) => Promise<DownloadAnswer>>;
}

/** Unless a spec says otherwise: the day lists three expiries, the chain is the fixture's, a download changes nothing. */
export function fakeChainService(spy: Spy, changes: Partial<FakeChainService> = {}): FakeChainService {
  return {
    day: spy.fn(async () => chainDay()),
    expiry: spy.fn(async () => chainExpiry()),
    download: spy.fn(async () => downloadAnswer([])),
    ...changes,
  };
}

/** Renders the Past · Archive chain behind a host that plays the address, so every pick comes back as an input. */
export async function renderArchive(service: FakeChainService, limits: SetupLimits | null = SERVED_LIMITS, start: { date?: string; symbol?: string } = {}) {
  const view = await render(ArchiveHostComponent, {
    providers: [
      ...fakePickerWorld().providers,
      { provide: PastChainService, useValue: service },
      { provide: OptionsBacktestService, useValue: { limits: async () => (limits === null ? Promise.reject(new Error('limits unavailable')) : limits) } },
    ],
  });
  const host = view.fixture.componentInstance;
  if (start.symbol !== undefined) host.symbol.set(start.symbol);
  if (start.date !== undefined) host.date.set(start.date);
  view.fixture.detectChanges();
  await view.fixture.whenStable();
  return { ...view, host };
}

/** Types a trading date into the shared date field and leaves it, as an owner does. */
export function pickDate(date: string): void {
  fireEvent.change(screen.getByLabelText('Trading date'), { target: { value: date } });
}

/** Types a whole time into the time box and leaves it, as a browser reports a time it has read. */
export function typeTime(text: string): void {
  fireEvent.change(screen.getByLabelText('Time of day (New York time)'), { target: { value: text } });
}

/** Types into the strikes box and leaves it, as an owner does: what is typed counts when the box is left. */
export function typeStrikes(text: string): void {
  const box = screen.getByLabelText('Strikes each side of the money');
  fireEvent.input(box, { target: { value: text } });
  fireEvent.blur(box);
}

/** Types into the strikes box and stays in it: what is typed counts only after a pause, or when the box is left. */
export function typeStrikesAndStay(text: string): void {
  fireEvent.input(screen.getByLabelText('Strikes each side of the money'), { target: { value: text } });
}

/** The ribbon button of an expiry: its day of the month, in the group of its month. */
export function ribbonButton(expiryDate: keyof typeof EXPIRY_MS): HTMLElement {
  const [, month, day] = expiryDate.split('-');
  const ribbon = document.querySelector('app-expiration-ribbon');
  if (ribbon === null) throw new Error('No expiry ribbon is on screen.');
  const monthName = MONTH_NAMES[month];
  const found = [...ribbon.querySelectorAll('button')].find(
    (button) => button.textContent?.trim() === day.replace(/^0/, '') && button.parentElement?.previousElementSibling?.textContent?.trim().startsWith(monthName),
  );
  if (found === undefined) throw new Error(`No ribbon button for ${expiryDate}.`);
  return found;
}

export function showChain(expiryDate: keyof typeof EXPIRY_MS): void {
  fireEvent.click(ribbonButton(expiryDate));
}

/** The chain's grid, once it is drawn. */
export const chainTable = () => screen.getByRole('table', { name: /Calls on the left/ });

/** Picks the standard date, waits for the day, opens the expiry on 2026-10-16 and waits for its chain. */
export async function openStandardChain(): Promise<void> {
  pickDate(TRADING_DATE);
  await screen.findByRole('region', { name: 'Expiries listed that day' });
  showChain('2026-10-16');
  await screen.findByRole('table', { name: /Calls on the left/ });
}

/** Like `openStandardChain` for a chain that draws no grid: it waits for the chain's facts instead of the table. */
export async function openChainCard(): Promise<void> {
  pickDate(TRADING_DATE);
  await screen.findByRole('region', { name: 'Expiries listed that day' });
  showChain('2026-10-16');
  await screen.findByText(/^Calls: \d+ listed/);
}

/** The row of the grid whose strike reads `strike` (the dollars as shown). */
export function rowOf(strike: string): HTMLElement {
  const row = [...chainTable().querySelectorAll<HTMLElement>('tbody tr')].find((candidate) => (candidate.querySelector('.strike-cell')?.textContent ?? '').trim().startsWith(strike));
  if (row === undefined) throw new Error(`No row of the chain reads ${strike}.`);
  return row;
}

/** What the call and the put of a row say, each cell read as text. */
export const callOf = (row: HTMLElement): string => reads(row.querySelector('td.call-side:not(.ls-cell)'));
export const putOf = (row: HTMLElement): string => reads(row.querySelector('td.put-side:not(.ls-cell)'));
