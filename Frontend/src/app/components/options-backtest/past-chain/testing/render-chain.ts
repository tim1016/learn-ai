import { fireEvent, render, screen, within } from '@testing-library/angular';
import type { Mock, vi as Vi } from 'vitest';

import { fakePickerWorld } from '../../../../shared/symbol-picker/testing/fake-picker-world';
import { PastChainTabComponent } from '../past-chain-tab.component';
import { PastChainService, type DayQuery, type ExpiryQuery } from '../past-chain.service';
import type { ChainDay, ChainExpiry, DownloadAnswer, DownloadRequest } from '../past-chain.types';
import { chainDay, chainExpiry, downloadAnswer, TRADING_DATE } from './fixtures';

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

export async function renderChainTab(service: FakeChainService, startingUnderlying = 'SPY') {
  const view = await render(PastChainTabComponent, {
    inputs: { startingUnderlying },
    providers: [...fakePickerWorld().providers, { provide: PastChainService, useValue: service }],
  });
  await view.fixture.whenStable();
  return view;
}

/** Types a trading date into the shared date field and leaves it, as an owner does. */
export function pickDate(date: string): void {
  fireEvent.change(screen.getByLabelText('Trading date'), { target: { value: date } });
}

export function typeTime(text: string): void {
  fireEvent.input(screen.getByLabelText('Time of day (New York time)'), { target: { value: text } });
}

export function typeStrikes(text: string): void {
  fireEvent.input(screen.getByLabelText('Strikes each side of the money'), { target: { value: text } });
}

/** The expiries table, once it is drawn. */
export const expiriesTable = () => screen.getByRole('table', { name: /Every expiry the listing held/ });

/** The row of the expiry on this date. */
export function expiryRow(date: string): HTMLElement {
  const table = within(expiriesTable());
  const row = table.getByRole('rowheader', { name: date }).closest('tr');
  if (row === null) throw new Error(`No expiry row reads ${date}.`);
  return row;
}

export function showChain(date: string): void {
  fireEvent.click(within(expiryRow(date)).getByRole('button', { name: /Show chain/ }));
}

/** The chain's table, once it is drawn. */
export const chainTable = () => screen.getByRole('table', { name: /Calls on the left/ });

/** Picks the standard date, waits for the day, opens the expiry on 2026-10-16 and waits for its chain. */
export async function openStandardChain(): Promise<void> {
  pickDate(TRADING_DATE);
  await screen.findByRole('table', { name: /Every expiry the listing held/ });
  showChain('2026-10-16');
  await screen.findByRole('table', { name: /Calls on the left/ });
}
