import { render, screen, waitFor, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { formatTimestampDisplay } from '../../shared/timestamp';
import type { SessionResult } from './options-golden-search.types';
import { OptionsSearchCandidateComponent } from './options-search-candidate.component';
import { candidate } from './testing';

const ENTRY_MS = 1791294000000;
const EXIT_MS = 1791294300000;
function session(): SessionResult {
  return { session_open_ms: ENTRY_MS, net_profit_usd: '19.95', status: 'traded', detail: 'One confirmed round trip.', attempts: [{ leg_id: 'short-call', attempt: 2, contract: 'O:SPY261009C00600000', side: 'sell', contracts: 1, entry_ms: ENTRY_MS, exit_ms: EXIT_MS, entry_price_usd: '2.50', exit_price_usd: '2.30', entry_fee_usd: '0.01234567890123456789', exit_fee_usd: '0.03765432109876543211', net_profit_usd: '19.95', exit_reason: 'stop_loss' }] };
}

describe('Options candidate leg-attempt evidence', () => {
  it('opens the setup session into exact leg fills, fees and anchored timestamps', async () => {
    await render(OptionsSearchCandidateComponent, { inputs: { candidate: candidate({ sessions: [session()] }) } });
    const user = userEvent.setup();
    await user.click(screen.getByText('Session, leg and attempt evidence'));
    await user.click(await screen.findByText(/Traded · 19.95 USD/));
    const cell = await screen.findByRole('cell', { name: 'O:SPY261009C00600000' });
    const row = within(cell.closest('tr') ?? screen.getByRole('table'));
    expect(row.getByRole('cell', { name: 'short-call' })).toBeTruthy();
    expect(row.getByRole('cell', { name: '0.01234567890123456789' })).toBeTruthy();
    expect(row.getByRole('cell', { name: '0.03765432109876543211' })).toBeTruthy();
    expect(row.getByText(formatTimestampDisplay(ENTRY_MS, { mode: 'et' }))).toBeTruthy();
    expect(row.getByText(formatTimestampDisplay(EXIT_MS, { mode: 'et' }))).toBeTruthy();
    expect(row.getByRole('cell', { name: 'Stop Loss' })).toBeTruthy();
  });

  it('fetches exact attempts only when their session opens and caches successful evidence', async () => {
    const sessionLoader = vi.fn(async () => session());
    const value = candidate({ sessions: [{ ...session(), attempts: [] }] });
    await render(OptionsSearchCandidateComponent, { inputs: { candidate: value, studyId: 'study-9', window: 'final', sessionLoader } });
    const user = userEvent.setup();
    expect(sessionLoader).not.toHaveBeenCalled();
    expect(screen.queryByRole('columnheader', { name: 'Entry fee (USD)' })).toBeNull();
    await user.click(screen.getByText('Session, leg and attempt evidence'));
    const summary = await screen.findByText(/Traded · 19.95 USD/);
    expect(sessionLoader).not.toHaveBeenCalled();
    await user.click(summary);
    await screen.findByRole('cell', { name: 'O:SPY261009C00600000' });
    expect(sessionLoader).toHaveBeenCalledExactlyOnceWith('study-9', value.candidate_hash, 'final', ENTRY_MS);
    await user.click(summary);
    await waitFor(() => expect(screen.queryByRole('cell', { name: 'O:SPY261009C00600000' })).toBeNull());
    await user.click(summary);
    await screen.findByRole('cell', { name: 'O:SPY261009C00600000' });
    expect(sessionLoader).toHaveBeenCalledTimes(1);
  });

  it('discards evidence resolving after the candidate changes', async () => {
    let resolve: ((value: SessionResult) => void) | undefined;
    const sessionLoader = vi.fn(() => new Promise<SessionResult>((done) => { resolve = done; }));
    const first = candidate({ sessions: [{ ...session(), attempts: [] }] });
    const view = await render(OptionsSearchCandidateComponent, { inputs: { candidate: first, studyId: 'study-9', sessionLoader } });
    const user = userEvent.setup();
    await user.click(screen.getByText('Session, leg and attempt evidence'));
    await user.click(await screen.findByText(/Traded · 19.95 USD/));
    await screen.findByText('Loading exact leg attempts…');
    view.fixture.componentRef.setInput('candidate', candidate({ candidate_hash: 'f'.repeat(64), sessions: [] }));
    view.fixture.detectChanges();
    resolve?.(session());
    await view.fixture.whenStable();
    expect(screen.queryByRole('cell', { name: 'O:SPY261009C00600000' })).toBeNull();
    expect(screen.queryByText('Loading exact leg attempts…')).toBeNull();
  });
});
