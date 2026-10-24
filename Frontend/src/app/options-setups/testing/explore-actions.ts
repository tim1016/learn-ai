import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import type { vi as Vi } from 'vitest';
import { expect } from 'vitest';

import type { SavedSetup, SetupRequest } from '../../components/options-backtest/options-backtest.types';
import { fakeService, type FakeService } from '../../components/options-backtest/testing/fake-service';
import { readBackOf, servedExamples } from '../../components/options-backtest/testing/fixtures';
import { press, typeInto } from '../../components/options-backtest/testing/page-actions';

/** The `vi` of the spec that calls: a shared helper never imports vitest itself. */
type Spy = Pick<typeof Vi, 'fn'>;

/**
 * A server that keeps the library a spec saves into: the shipped examples, then what was saved, each as it was read back.
 * `save` replaces the setup under its name, as the real route does, and the next `list` serves it.
 */
export function libraryWorld(spy: Spy, changes: Partial<FakeService> = {}): { library: Map<string, SetupRequest>; service: FakeService } {
  const library = new Map<string, SetupRequest>();
  const entry = (name: string, setup: SetupRequest): SavedSetup => ({ name, example: false, saved_at_ms: 1_760_000_000_000, cannot_open_reason: null, setup: readBackOf(setup) });
  const service = fakeService(spy, {
    list: spy.fn(async (): Promise<SavedSetup[]> => [...servedExamples(), ...[...library].map(([name, setup]) => entry(name, setup))]),
    save: spy.fn(async (name: string, setup: SetupRequest): Promise<SavedSetup> => {
      library.set(name, setup);
      return entry(name, setup);
    }),
    remove: spy.fn(async (name: string) => {
      library.delete(name);
    }),
    ...changes,
  });
  return { library, service };
}

/** One tab of the setup's place, by its name in the strip: Overview, Define or Explore. */
export function tabLink(name: 'Overview' | 'Define' | 'Explore'): HTMLElement {
  return within(screen.getByRole('navigation', { name: 'Options setup tabs' })).getByRole('link', { name });
}

/** Goes to Define and back, or the other way, by the strip, as the owner does, and waits for the tab to be drawn. */
export async function goToExplore(): Promise<void> {
  fireEvent.click(tabLink('Explore'));
  await screen.findByRole('navigation', { name: 'Explore views' });
}

export async function goToDefine(): Promise<void> {
  fireEvent.click(tabLink('Define'));
  await screen.findByRole('heading', { name: 'Entry settings' });
}

/** Types a slippage into Define, which changes the draft and nothing saved. */
export function editSlippage(value: string): void {
  typeInto('Slippage (percent)', value);
}

/** Saves what Define holds under the name it holds, and waits for the word that it was saved. */
export async function saveInDefine(name: string): Promise<void> {
  await waitFor(() => expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Save setup' }).disabled).toBe(false));
  press('Save setup');
  await screen.findByText(`Saved “${name}”.`);
}
