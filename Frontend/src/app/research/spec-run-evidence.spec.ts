import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { screen, within } from '@testing-library/angular';
import { beforeEach, describe, expect, it } from 'vitest';

import { environment } from '../../environments/environment';
import { routes } from '../app.routes';
import { activeMenuNodeFor } from '../shell/app-menu';
import { makeRunLedger, makeRunResponse } from '../testing/strategy-run-fixtures';

/**
 * A `StrategySpec` run and its Monte Carlo, baseline and walk-forward children
 * are Research evidence (PRD #3490 R27): each opens at its native page under
 * `/research/history`, from its old link, and is keyed by its own id.
 */
describe('spec-run evidence', () => {
  let harness: RouterTestingHarness;
  let http: HttpTestingController;

  beforeEach(async () => {
    TestBed.configureTestingModule({
      providers: [provideRouter(routes), provideHttpClient(), provideHttpClientTesting()],
    });
    harness = await RouterTestingHarness.create();
    http = TestBed.inject(HttpTestingController);
  });

  it.each([
    ['a spec run', '/research-lab/strategy-runs/run-7', '/research/history/spec-run/run-7', 'Run run-7'],
    ['a walk-forward', '/research-lab/walk-forward/wf-7', '/research/history/walk-forward/wf-7', 'Walk-forward wf-7'],
    ['a Monte Carlo run', '/research-lab/monte-carlo/mc-7', '/research/history/monte-carlo/mc-7', 'Monte Carlo mc-7'],
    ['a baseline', '/research-lab/baselines/bl-7', '/research/history/baseline/bl-7', 'Baseline bl-7'],
  ])('opens %s from its old link at its own page under Research, keeping query and fragment', async (_name, oldLink, native, heading) => {
    await harness.navigateByUrl(`${oldLink}?tab=trades#table`);

    expect.soft(TestBed.inject(Router).url).toBe(`${native}?tab=trades#table`);
    expect.soft(screen.getByRole('heading', { level: 1, name: heading })).toBeTruthy();
    expect.soft(activeMenuNodeFor(TestBed.inject(Router).url)?.group.title).toBe('Research');
  });

  it('shows a spec run named like an EMA strategy as Research evidence, never under the EMA strategy', async () => {
    await harness.navigateByUrl('/research-lab/strategy-runs/ema-run');
    http
      .expectOne(`${environment.pythonServiceUrl}/api/research/strategy-runs/ema-run`)
      .flush(makeRunResponse(makeRunLedger({ run_id: 'ema-run', strategy_spec_id: 'spy_ema_crossover' })));

    const page = within(harness.routeNativeElement as HTMLElement);
    expect.soft(await page.findByRole('heading', { level: 1, name: /spy_ema_crossover/ }), 'named by its own spec id').toBeTruthy();
    expect.soft(TestBed.inject(Router).url).toBe('/research/history/spec-run/ema-run');
    expect.soft(activeMenuNodeFor(TestBed.inject(Router).url)?.group.title).toBe('Research');

    const hrefs = page.getAllByRole('link').map((link) => link.getAttribute('href') ?? '');
    expect(
      hrefs.filter((href) => href.startsWith('/strategy-lab') || href.startsWith('/signal-strategies')),
      'no link into a registered strategy',
    ).toEqual([]);
  });

  it('fails in place for a run that is not stored, naming the id and offering the way back', async () => {
    await harness.navigateByUrl('/research/history/spec-run/missing-run');
    http
      .expectOne(`${environment.pythonServiceUrl}/api/research/strategy-runs/missing-run`)
      .flush({ detail: 'not found' }, { status: 404, statusText: 'Not Found' });

    const page = within(harness.routeNativeElement as HTMLElement);
    expect.soft(await page.findByText(/404/), 'says what went wrong').toBeTruthy();
    expect.soft(TestBed.inject(Router).url, 'stays at the address it was given').toBe('/research/history/spec-run/missing-run');
    expect.soft(page.getByRole('heading', { level: 1, name: 'Run missing-run' })).toBeTruthy();
    expect.soft(page.getByRole('link', { name: '← History' }).getAttribute('href')).toBe('/research/history?type=spec-run');
  });
});
