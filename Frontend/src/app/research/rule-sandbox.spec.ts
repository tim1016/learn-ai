import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { routes } from '../app.routes';
import { CANONICAL_FIXTURES } from '../components/spec-strategy-runner/canonical-fixtures';
import type { SavedStrategy } from '../components/spec-strategy-runner/strategy-store.service';
import { provideFakeDateWorld } from '../shared/date-picker/testing/fake-trading-calendar';

/**
 * T21 (PRD #3490): a spec saved in this browser opens, with its content, at the
 * old address and at the new one. The key below is the one the store has
 * always written, so a spec saved before the move needs no migration.
 */
const SAVED_SPECS_KEY = 'learn-ai.spec-strategy.saved.v1';

const SMA_CROSSOVER = CANONICAL_FIXTURES.find((fixture) => fixture.id === 'sma_crossover');
if (SMA_CROSSOVER === undefined) throw new Error('The sma_crossover canonical fixture is gone.');

const SAVED: SavedStrategy = {
  id: 'saved-qqq',
  name: 'QQQ pullback rules',
  createdAt: 1_760_000_000_000,
  updatedAt: 1_760_000_100_000,
  spec: { ...structuredClone(SMA_CROSSOVER.spec), name: 'QQQ pullback rules', symbols: ['QQQ'] },
};

describe('the Rule sandbox', () => {
  let harness: RouterTestingHarness;

  beforeEach(async () => {
    localStorage.setItem(SAVED_SPECS_KEY, JSON.stringify([SAVED]));
    TestBed.configureTestingModule({
      providers: [provideRouter(routes), provideHttpClient(), provideHttpClientTesting(), ...provideFakeDateWorld()],
    });
    harness = await RouterTestingHarness.create();
  });

  afterEach(() => {
    localStorage.clear();
  });

  it.each([
    ['the old address', '/spec-strategy?probe=1#probe'],
    ['the new address', '/research/rule-sandbox?probe=1#probe'],
  ])('opens a spec saved in this browser through %s with its content intact', async (_name, url) => {
    await harness.navigateByUrl(url);
    expect(TestBed.inject(Router).url).toBe('/research/rule-sandbox?probe=1#probe');

    await userEvent.selectOptions(screen.getByLabelText('Load a saved strategy'), SAVED.id);
    harness.detectChanges();

    expect.soft((screen.getByLabelText('Strategy name') as HTMLInputElement).value).toBe(SAVED.name);
    expect.soft(screen.getByText('15-min · QQQ'), 'its symbol').toBeTruthy();
    expect
      .soft(screen.getAllByLabelText('Indicator id').map((input) => (input as HTMLInputElement).value), 'its indicators')
      .toEqual(['sma_s', 'sma_l']);
    expect
      .soft(screen.getAllByLabelText('Indicator period').map((input) => (input as HTMLInputElement).value), 'their periods')
      .toEqual(['10', '30']);
  });

  it('says it is not deployable and offers no Deploy', async () => {
    await harness.navigateByUrl('/research/rule-sandbox');

    expect.soft(screen.getByRole('heading', { level: 1, name: 'Rule sandbox' })).toBeTruthy();
    expect.soft(screen.getByTestId('not-deployable').textContent).toContain('Not deployable');

    const deployActions = [...screen.queryAllByRole('button'), ...screen.queryAllByRole('link')].filter(
      (action) => /deploy/i.test(`${action.textContent} ${action.getAttribute('href') ?? ''}`),
    );
    expect(deployActions, 'no button or link that deploys').toEqual([]);
  });
});
