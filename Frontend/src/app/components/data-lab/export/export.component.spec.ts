import { signal } from '@angular/core';
import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
  type TestRequest,
} from '@angular/common/http/testing';
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

const PLAN_URL = `${environment.pythonServiceUrl}/api/dataset/plan`;
const COLUMNS = ['open', 'high', 'low', 'close', 'volume', 'rsi_length14'];

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
  store.setExportTimeZone('America/Chicago');
  store.patchDraft({ ticker: 'SPY', window: WINDOW });
  store.commitScope();
  store.addIndicator('rsi', { length: 14 });
  return { ...result, http, store, runSession };
}

/** Answer the plan request the page issued by itself. */
async function answerPlan(
  http: HttpTestingController,
  receipt: Record<string, unknown> = { session_count: 21, output_columns: COLUMNS },
): Promise<TestRequest> {
  const request = await waitFor(() => http.expectOne(PLAN_URL));
  request.flush(receipt);
  return request;
}

async function generatePayload(runSession: ReturnType<typeof mockRunSession>) {
  await userEvent.click(await screen.findByRole('button', { name: 'Generate dataset ZIP' }));
  await waitFor(() => expect(runSession.start).toHaveBeenCalledTimes(1));
  return runSession.start.mock.calls[0][0] as Record<string, unknown>;
}

describe('ExportComponent', () => {
  it('never mounts the chart and never calls the chart endpoint', async () => {
    const { http } = await renderExport();
    expect(document.querySelector('app-data-lab-chart')).toBeNull();

    await answerPlan(http);
    expect(http.match(`${environment.pythonServiceUrl}/api/chart/data`).length).toBe(0);
    http.verify();
  });

  it('loads the plan by itself and renders the receipt verbatim', async () => {
    const { http } = await renderExport();
    await answerPlan(http, {
      ticker: 'SPY',
      window_start_ms_utc: WINDOW.startMsUtc,
      window_end_ms_utc: WINDOW.endMsUtc,
      exchange_sessions: ['2026-04-15', '2026-04-16', '2026-04-17'],
      session_count: 21,
      output_columns: COLUMNS,
      output_column_count: 6,
      time_column: 'time_america_chicago',
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
    expect(await within(receipt).findByText('8946')).toBeTruthy();
    // Every planned column is offered exactly as the server named it.
    for (const column of COLUMNS) {
      expect(await screen.findByRole('checkbox', { name: column })).toBeTruthy();
    }
    expect(screen.getByText('unix_ts, time_america_chicago')).toBeTruthy();
    expect(screen.getByText('all 6')).toBeTruthy();
    expect(await screen.findByText(/Allowed: 15m, 1h, 4h, 1D/)).toBeTruthy();
    expect(await screen.findByText(/Recommended: 1D/)).toBeTruthy();
    // The zone dropdown also lists America/New_York — scope to the receipt.
    expect(await within(receipt).findByText('America/New_York')).toBeTruthy();
    // Server-authored warning prose renders directly — no receiptLabel pipe.
    expect(
      await screen.findByText('Tick-level volume is estimated from minute aggregates.'),
    ).toBeTruthy();
    http.verify();
  });

  it('asks the plan for the chosen time zone and never sends it the column selection', async () => {
    const { http, store, fixture } = await renderExport();
    const first = await answerPlan(http);
    expect(first.request.body['time_zone']).toBe('America/Chicago');
    expect('columns' in first.request.body).toBe(false);

    // Ticking a box re-plans nothing; changing the zone re-plans.
    await userEvent.click(await screen.findByRole('checkbox', { name: 'high' }));
    fixture.detectChanges();
    http.expectNone(PLAN_URL);
    store.setExportTimeZone('UTC');
    const second = await answerPlan(http, { output_columns: COLUMNS, time_column: 'time_utc' });
    expect(second.request.body['time_zone']).toBe('UTC');
    expect(await screen.findByText('unix_ts, time_utc')).toBeTruthy();
    http.verify();
  });

  it('disables generate while a run is active', async () => {
    const { runSession } = await renderExport(mockRunSession('fetching'));
    const button = screen.getByRole('button', { name: /Run in progress/ });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(runSession.start).not.toHaveBeenCalled();
  });

  it('waits for the column list to finish updating before generating', async () => {
    const { http, runSession } = await renderExport();
    const button = await screen.findByRole('button', { name: 'Updating column list…' });
    expect((button as HTMLButtonElement).disabled).toBe(true);

    await answerPlan(http);
    await generatePayload(runSession);
    http.verify();
  });

  it('submits generation only through RunSessionService.start with the mapped payload', async () => {
    const { http, runSession } = await renderExport();
    await answerPlan(http);
    const payload = await generatePayload(runSession);

    expect(payload['ticker']).toBe('SPY');
    expect(payload['start_ms_utc']).toBe(WINDOW.startMsUtc);
    expect(payload['end_ms_utc']).toBe(WINDOW.endMsUtc);
    expect(payload['indicator_entries']).toEqual([{ name: 'rsi', params: { length: 14 } }]);
    expect(payload['adjust_for_dividends']).toBe(false);
    // Untouched checkboxes export every column, including later ones.
    expect(payload['columns']).toBeNull();
    expect(payload['time_zone']).toBe('America/Chicago');
    // No HTTP of its own beyond the plan — generation is job-backed only (FR-005).
    http.verify();
  });

  it('exports only the ticked columns and no readable time when the zone is None', async () => {
    const { http, runSession } = await renderExport();
    await answerPlan(http);
    await userEvent.click(await screen.findByRole('checkbox', { name: 'high' }));
    await userEvent.selectOptions(screen.getByRole('combobox', { name: /Readable time column/ }), 'None');
    await answerPlan(http);
    expect(screen.getByText('5 of 6')).toBeTruthy();

    const payload = await generatePayload(runSession);
    expect(payload['columns']).toEqual(['open', 'low', 'close', 'volume', 'rsi_length14']);
    expect(payload['time_zone']).toBeNull();
    http.verify();
  });

  it('sends adjust_for_dividends when the toggle is on', async () => {
    const { http, runSession } = await renderExport();
    await answerPlan(http);
    await userEvent.click(screen.getByRole('checkbox', { name: /server-side dividend adjustment/ }));
    await answerPlan(http);
    const payload = await generatePayload(runSession);
    expect(payload['adjust_for_dividends']).toBe(true);
  });

  it('re-plans by itself when the recipe changes; a new column starts unticked after an edit', async () => {
    const { http, store, runSession } = await renderExport();
    await answerPlan(http);
    await userEvent.click(await screen.findByRole('checkbox', { name: 'high' }));

    store.addIndicator('atr', { length: 14 });
    await answerPlan(http, { output_columns: [...COLUMNS, 'atr_length14'] });
    const atr = (await screen.findByRole('checkbox', { name: 'atr_length14' })) as HTMLInputElement;
    expect(atr.checked).toBe(false);
    expect(screen.queryByText(/out of date/i)).toBeNull();

    const payload = await generatePayload(runSession);
    expect(payload['columns']).toEqual(['open', 'low', 'close', 'volume', 'rsi_length14']);
    http.verify();
  });

  it('ignores a slower answer to an older plan request', async () => {
    const { http, store } = await renderExport();
    const older = await waitFor(() => http.expectOne(PLAN_URL));
    // The first request is still pending when the recipe changes.
    store.addIndicator('atr', { length: 14 });
    const newer = await waitFor(() => http.expectOne(PLAN_URL));
    newer.flush({ output_columns: [...COLUMNS, 'atr_length14'] });
    older.flush({ output_columns: COLUMNS });

    expect(await screen.findByRole('checkbox', { name: 'atr_length14' })).toBeTruthy();
    expect(screen.queryByText(/out of date/i)).toBeNull();
    http.verify();
  });

  it('labels the receipt out of date and offers Try again when a re-plan fails', async () => {
    const { http, store } = await renderExport();
    await answerPlan(http);
    store.addIndicator('atr', { length: 14 });
    const failed = await waitFor(() => http.expectOne(PLAN_URL));
    failed.flush({ detail: 'boom' }, { status: 500, statusText: 'Server Error' });

    expect((await screen.findAllByText(/out of date/i)).length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await answerPlan(http, { output_columns: [...COLUMNS, 'atr_length14'] });
    await waitFor(() => expect(screen.queryByText(/out of date/i)).toBeNull());
    http.verify();
  });
});
