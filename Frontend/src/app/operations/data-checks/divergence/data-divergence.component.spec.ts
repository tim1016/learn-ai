import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { environment } from '../../../../environments/environment';
import { DataDivergenceComponent } from './data-divergence.component';

const BASE = `${environment.pythonServiceUrl}/research/data-divergence`;

function matrix(timeframe: string) {
  return {
    timeframe,
    rows: [{ indicator: 'ema_20', impl: 'native', n: 1200, median_abs: 0.0012, p95_abs: 0.004, max_abs: 0.02, corr: 0.9999 }],
  };
}

async function renderDivergence() {
  await render(DataDivergenceComponent, { providers: [provideHttpClient(), provideHttpClientTesting()] });
  return TestBed.inject(HttpTestingController);
}

describe('DataDivergenceComponent', () => {
  it('opens by reading the 15 minute matrix and rebuilds nothing', async () => {
    const http = await renderDivergence();

    const read = http.expectOne(`${BASE}/matrix/15m`);
    expect(read.request.method).toBe('GET');
    read.flush(matrix('15m'));

    expect(await screen.findByText('ema_20')).toBeTruthy();
    http.expectNone(`${BASE}/rebuild?tf=15m`);
    http.verify();
  });

  it('rebuilds the dashboard only when asked, then reads the matrix again', async () => {
    const http = await renderDivergence();
    http.expectOne(`${BASE}/matrix/15m`).flush(matrix('15m'));

    await userEvent.click(screen.getByRole('button', { name: 'Rebuild dashboard' }));
    const rebuild = http.expectOne(`${BASE}/rebuild?tf=15m`);
    expect(rebuild.request.method).toBe('POST');
    rebuild.flush({ path: 'cache/divergence/15m/dashboard_15m.html', size_mb: 1.5, timeframe: '15m' });

    expect(await screen.findByText(/Rebuilt 15m dashboard/)).toBeTruthy();
    await waitFor(() => http.expectOne(`${BASE}/matrix/15m`).flush(matrix('15m')));
    http.verify();
  });

  it('says a rebuild failed instead of showing the old dashboard as fresh', async () => {
    const http = await renderDivergence();
    http.expectOne(`${BASE}/matrix/15m`).flush(matrix('15m'));

    await userEvent.click(screen.getByRole('button', { name: 'Rebuild dashboard' }));
    http.expectOne(`${BASE}/rebuild?tf=15m`).flush({ detail: 'Cache not populated' }, { status: 409, statusText: 'Conflict' });

    expect((await screen.findByRole('alert')).textContent).toContain('Rebuild failed');
    expect(screen.queryByText(/Rebuilt 15m dashboard/)).toBeNull();
  });

  it('reads the matrix for the timeframe it is switched to', async () => {
    const http = await renderDivergence();
    http.expectOne(`${BASE}/matrix/15m`).flush(matrix('15m'));

    await userEvent.selectOptions(screen.getByRole('combobox'), '1h');

    http.expectOne(`${BASE}/matrix/1h`).flush({ detail: 'Matrix for 1h not computed' }, { status: 404, statusText: 'Not Found' });
    expect(await screen.findByText(/No matrix data for/)).toBeTruthy();
  });
});
