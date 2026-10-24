import { fireEvent, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { exampleBody } from '../../../components/options-backtest/testing/fixtures';
import { declarations, historyGuidance } from '../../../components/options-golden-search/testing';
import { libraryWorld } from '../../testing/explore-actions';
import { indexOf, savedName } from '../../testing/fixtures';
import { renderTab } from '../../testing/render-workspace';

/** The services a search reads before anything is checked: the saved setup's declarations, and the date guidance of its rules. */
function searchWorld() {
  return {
    declarations: vi.fn(async (_name: string) => ({ ...declarations(), setup_name: 'Mine' })),
    historyGuidance: vi.fn(async () => historyGuidance()),
    preflight: vi.fn(),
    create: vi.fn(),
    command: vi.fn(),
    captureHistory: vi.fn(),
    activeHistoryCapture: vi.fn(() => null),
    refreshHistoryCaptures: vi.fn(async () => {}),
    onHistoryCapture: vi.fn(() => () => {}),
    cancelHistoryCapture: vi.fn(),
  };
}

async function golden(query = '') {
  const library = libraryWorld(vi);
  library.library.set('Mine', exampleBody());
  const search = searchWorld();
  await renderTab(library.service, 'Mine', 'golden', {
    query,
    index: async () => indexOf({ setups: [savedName('Mine', '1')] }),
    golden: { get: vi.fn(), list: async () => [] },
    evidence: { read: vi.fn() },
    search,
  });
  return search;
}

describe('starting a golden search from the Golden tab', () => {
  it('reads nothing of the search until the plan is asked for, and then reads the saved setup the address names', async () => {
    const search = await golden();
    expect(search.declarations).not.toHaveBeenCalled();

    fireEvent.click(await screen.findByRole('button', { name: 'Set up a golden search' }));

    await screen.findByText(/Original identity/);
    expect(search.declarations).toHaveBeenCalledExactlyOnceWith('Mine');
    expect(screen.getByRole('button', { name: 'Close the plan' }).getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText(/Research only. Starting a search does not place orders./)).toBeTruthy();
  });

  it('opens the plan at once for a link that forks a study, and names that study', async () => {
    const search = await golden('?parent_study_id=os_a');

    await screen.findByText(/Original identity/);
    expect(search.declarations).toHaveBeenCalledExactlyOnceWith('Mine');
    expect(screen.getByRole('link', { name: 'an existing study' }).getAttribute('href')).toBe('/options-setups/evidence/study/os_a');
  });

  it('closes the plan on the owner’s word', async () => {
    await golden();
    fireEvent.click(await screen.findByRole('button', { name: 'Set up a golden search' }));
    await screen.findByText(/Original identity/);

    fireEvent.click(screen.getByRole('button', { name: 'Close the plan' }));

    await waitFor(() => expect(screen.queryByText(/Original identity/)).toBeNull());
  });
});
