import { signal } from '@angular/core';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { render, screen, waitFor, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { environment } from '../../../../environments/environment';
import { RunSessionService } from '../../../services/run-session.service';
import { ExportComponent } from './export.component';
import { createDataLabWorkspaceStore, DataLabWorkspaceStore } from '../data-lab-workspace-store';

const WINDOW = {
  startMsUtc: Date.UTC(2026, 3, 15),
  endMsUtc: Date.UTC(2026, 4, 15),
};

function mockRunSession(state: 'idle' | 'fetching' = 'idle') {
  return {
    state: signal<'idle' | 'fetching' | 'bundling'>(state),
    sessionId: signal<string | null>(null),
    dockState: signal<'idle' | 'active' | 'done' | 'error'>(state === 'idle' ? 'idle' : 'active'),
    start: vi.fn(async (_payload: Record<string, unknown>) => undefined),
  };
}

async function renderExport(runSession = mockRunSession()) {
  const result = await render(ExportComponent, {
    providers: [
      provideRouter([]),
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: RunSessionService, useValue: runSession },
      { provide: DataLabWorkspaceStore, useFactory: createDataLabWorkspaceStore },
    ],
  });
  const http = TestBed.inject(HttpTestingController);
  const store = result.fixture.componentInstance.store;
  store.patchDraft({ ticker: 'SPY', window: WINDOW });
  store.commitScope();
  store.addIndicator('rsi', { length: 14 });
  return { ...result, http, store, runSession };
}

describe('ExportComponent', () => {
  it('never mounts the chart and never calls the chart endpoint', async () => {
    const { http } = await renderExport();
    expect(document.querySelector('app-data-lab-chart')).toBeNull();

    // Exercise the form surfaces, then prove zero requests of any kind
    // besides the plan endpoint.
    await userEvent.click(screen.getByRole('button', { name: /Preview columns/ }));
    const plan = await waitFor(() =>
      http.expectOne(`${environment.pythonServiceUrl}/api/dataset/plan`),
    );
    expect(
      http.match(`${environment.pythonServiceUrl}/api/chart/data`).length,
    ).toBe(0);
    plan.flush({ session_count: 20, output_column_count: 7 });
    expect(
      http.match(`${environment.pythonServiceUrl}/api/chart/data`).length,
    ).toBe(0);
    http.verify();
  });

  it('renders the plan receipt verbatim without recomputation', async () => {
    const { http } = await renderExport();
    await userEvent.click(screen.getByRole('button', { name: /Preview columns/ }));
    const plan = await waitFor(() =>
      http.expectOne(`${environment.pythonServiceUrl}/api/dataset/plan`),
    );
    plan.flush({
      session_count: 21,
      output_column_count: 9,
      output_columns: ['unix_ts', 'open', 'high', 'low', 'close', 'volume', 'rsi_14'],
      warnings: ['NON_TRADING_WINDOW'],
      estimates: { polygon_requests: 4 },
      estimate_assumptions: { plan: 'starter' },
      estimate_provenance: { source: 'python' },
      timeframe_advice: { recommended: '5m' },
      exchange: 'NYSE',
      timezone: 'America/New_York',
      calendar_version: '2026.1',
    });

    // Scope to the receipt region: the run summary at the bottom also
    // mirrors session_count, so a global text query for '21' is ambiguous.
    const receipt = await screen.findByRole('region', { name: 'Dataset plan receipt' });
    expect(await within(receipt).findByText('21')).toBeTruthy();
    expect(await screen.findByText(/unix_ts, open, high, low, close, volume, rsi_14/)).toBeTruthy();
    expect(await screen.findByText(/polygon_requests/)).toBeTruthy();
    // Backend receipt codes render through the receiptLabel pipe, which
    // title-cases code segments: NON_TRADING_WINDOW → "Non Trading Window".
    expect(await screen.findByText('Non Trading Window')).toBeTruthy();
    http.verify();
  });

  it('disables generate while a run is active', async () => {
    const { runSession } = await renderExport(mockRunSession('fetching'));
    const button = screen.getByRole('button', { name: /Run in progress/ });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(runSession.start).not.toHaveBeenCalled();
  });

  it('submits generation only through RunSessionService.start with the mapped payload', async () => {
    const { http, runSession } = await renderExport();
    await userEvent.click(screen.getByRole('button', { name: 'Generate dataset ZIP' }));

    await waitFor(() => expect(runSession.start).toHaveBeenCalledTimes(1));
    const payload = runSession.start.mock.calls[0][0] as Record<string, unknown>;
    expect(payload['ticker']).toBe('SPY');
    expect(payload['start_ms_utc']).toBe(WINDOW.startMsUtc);
    expect(payload['end_ms_utc']).toBe(WINDOW.endMsUtc);
    expect(payload['indicator_entries']).toEqual([{ name: 'rsi', params: { length: 14 } }]);
    // No HTTP of its own — generation is job-backed only (FR-005).
    http.verify();
  });
});
