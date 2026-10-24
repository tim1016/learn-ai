import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { environment } from '../../../environments/environment';
import { SpyEmaFixtureRunComponent } from './spy-ema-fixture-run.component';

const FIXTURE_URL = `${environment.pythonServiceUrl}/api/spec-strategy/fixtures/spy_ema_crossover`;
const RUNS_URL = `${environment.pythonServiceUrl}/api/research/strategy-runs`;
const RUN_ID = 'a1b2c3d4e5f60718293a4b5c6d7e8f90';

async function renderTool() {
  await render(SpyEmaFixtureRunComponent, {
    providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
  });
  return TestBed.inject(HttpTestingController);
}

describe('SpyEmaFixtureRunComponent', () => {
  it('starts nothing until the button is pressed', async () => {
    const http = await renderTool();

    expect(screen.getByRole('button', { name: 'Run SPY EMA fixture' })).toBeTruthy();
    http.expectNone(() => true);
  });

  it('runs the canonical spec once and links to the run it persisted', async () => {
    const http = await renderTool();

    await userEvent.click(screen.getByRole('button', { name: 'Run SPY EMA fixture' }));
    http.expectOne(FIXTURE_URL).flush({ name: 'spy_ema_crossover' });
    const post = await waitFor(() => http.expectOne(RUNS_URL));
    expect(post.request.method).toBe('POST');
    expect(post.request.body).toMatchObject({
      spec: { name: 'spy_ema_crossover' },
      strategy_spec_id: 'spy_ema_crossover',
      start_date: '2024-01-02',
      end_date: '2024-12-31',
    });
    post.flush({ ledger: { run_id: RUN_ID }, result: {} });

    const link = await screen.findByRole('link', { name: `${RUN_ID.slice(0, 12)}…` });
    expect(link.getAttribute('href')).toBe(`/research/history/spec-run/${RUN_ID}`);
    http.verify();
  });

  it('says so when the run fails, and offers the button again', async () => {
    const http = await renderTool();

    await userEvent.click(screen.getByRole('button', { name: 'Run SPY EMA fixture' }));
    http.expectOne(FIXTURE_URL).flush('no fixture', { status: 500, statusText: 'Server Error' });

    expect(await screen.findByText(/Http failure response/)).toBeTruthy();
    expect(screen.queryByText(/Fixture run completed/)).toBeNull();
    await waitFor(() => expect((screen.getByRole('button', { name: 'Run SPY EMA fixture' }) as HTMLButtonElement).disabled).toBe(false));
  });
});
