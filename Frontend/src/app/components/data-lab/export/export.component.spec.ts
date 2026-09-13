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
      ticker: 'SPY',
      window_start_ms_utc: WINDOW.startMsUtc,
      window_end_ms_utc: WINDOW.endMsUtc,
      exchange_sessions: ['2026-04-15', '2026-04-16', '2026-04-17'],
      session_count: 21,
      output_columns: ['unix_ts', 'open', 'high', 'low', 'close', 'volume', 'rsi_14'],
      output_column_count: 9,
      estimated_bars: 8946,
      estimate_assumptions: ['~391 rth bars per daily session', 'no half days modeled'],
      estimate_provenance: 'pandas-market-calendars NYSE schedule',
      companion_dependencies: [],
      warnings: ['Tick-level volume is estimated from minute aggregates.'],
      allowed_timeframes: ['15m', '1h', '4h', '1D'],
      recommended_timeframes: ['1D'],
      exchange: 'NYSE',
      calendar_timezone: 'America/New_York',
      calendar_version: '2026.1',
    });

    // Scope to the receipt region: the run summary at the bottom also
    // mirrors session_count, so a global text query for '21' is ambiguous.
    const receipt = await screen.findByRole('region', { name: 'Dataset plan receipt' });
    expect(await within(receipt).findByText('21')).toBeTruthy();
    expect(await screen.findByText(/unix_ts, open, high, low, close, volume, rsi_14/)).toBeTruthy();
    // Estimated bars / timeframe advice / timezone render from the real
    // DatasetPlanResponse contract fields.
    expect(await within(receipt).findByText('8946')).toBeTruthy();
    expect(await screen.findByText(/Allowed: 15m, 1h, 4h, 1D/)).toBeTruthy();
    expect(await screen.findByText(/Recommended: 1D/)).toBeTruthy();
    expect(await screen.findByText('America/New_York')).toBeTruthy();
    // Server-authored warning prose renders directly — no receiptLabel pipe.
    expect(
      await screen.findByText('Tick-level volume is estimated from minute aggregates.'),
    ).toBeTruthy();
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
    expect(payload['adjust_for_dividends']).toBe(false);
    // No HTTP of its own — generation is job-backed only (FR-005).
    http.verify();
  });

  it('sends adjust_for_dividends when the toggle is on', async () => {
    const { runSession } = await renderExport();
    await userEvent.click(screen.getByRole('checkbox', { name: /server-side dividend adjustment/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Generate dataset ZIP' }));
    await waitFor(() => expect(runSession.start).toHaveBeenCalledTimes(1));
    const payload = runSession.start.mock.calls[0][0] as Record<string, unknown>;
    expect(payload['adjust_for_dividends']).toBe(true);
  });

  it('marks the plan receipt stale when the recipe changes after the plan ran', async () => {
    const { http, store, fixture } = await renderExport();
    await userEvent.click(screen.getByRole('button', { name: /Preview columns/ }));
    const plan = await waitFor(() =>
      http.expectOne(`${environment.pythonServiceUrl}/api/dataset/plan`),
    );
    plan.flush({ session_count: 20, output_column_count: 7 });
    await waitFor(() => expect(store.datasetPlanReceipt()).not.toBeNull());
    expect(screen.queryByText(/out of date/i)).toBeNull();

    // Any recipe mutation (indicator set here) invalidates the receipt: the
    // section must be labeled stale instead of presenting old counts as
    // current plan data.
    store.addIndicator('atr', { length: 14 });
    fixture.detectChanges();
    expect(screen.getAllByText(/out of date/i).length).toBeGreaterThan(0);

    // Re-running the plan for the current recipe clears the staleness.
    await userEvent.click(screen.getByRole('button', { name: /Preview columns/ }));
    const replan = await waitFor(() =>
      http.expectOne(`${environment.pythonServiceUrl}/api/dataset/plan`),
    );
    replan.flush({ session_count: 20, output_column_count: 9 });
    await waitFor(() =>
      expect(screen.queryByText(/out of date/i)).toBeNull(),
    );
    http.verify();
  });
});
