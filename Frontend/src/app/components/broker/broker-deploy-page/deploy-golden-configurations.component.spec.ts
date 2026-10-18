import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { JobsService } from '../../../services/jobs.service';
import type { GoldenQualificationSummary } from '../../golden-search/golden-search.types';
import { EMA_STRATEGY } from './alpaca-deploy-workflow.fixtures';
import { DeployGoldenConfigurationsComponent } from './deploy-golden-configurations.component';

const BASE = '/api/research/golden-qualifications';
const SAVED: GoldenQualificationSummary = {
  id: 'gq-saved', program_key: 'ema_crossover_signal', program_version: 'ema-crossover-signal/v1',
  symbol: 'SPY', parameters: { gap: 0.15 }, status: 'stale', status_explanation: 'Revalidate the saved evidence.',
  is_default: false, note: 'Earlier settings', approved_by: 'owner', created_at_ms: 1759300000000,
  study_id: 'study-saved', golden_run_id: 7, research: {},
};

async function setup() {
  const selected = vi.fn();
  const rendered = await render(DeployGoldenConfigurationsComponent, {
    inputs: { strategies: [EMA_STRATEGY] }, on: { selected },
    providers: [provideHttpClient(), provideHttpClientTesting(), { provide: JobsService, useValue: {} }],
  });
  const http = TestBed.inject(HttpTestingController);
  return { ...rendered, http, selected };
}

describe('Saved signal configurations', () => {
  it('keeps failed proof blocked, then offers only the server-revalidated version', async () => {
    const { http, selected } = await setup();
    http.expectOne(`${BASE}?limit=500`).flush([SAVED]);
    await screen.findByRole('option', { name: /run 7/ });
    fireEvent.change(screen.getByLabelText('Saved signal configuration'), { target: { value: SAVED.id } });
    const apply = await screen.findByRole('button', { name: 'Use selected golden configuration' });
    expect((apply as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Revalidate saved configuration' }));
    const refused = http.expectOne(`${BASE}/${SAVED.id}/reprove`);
    expect(refused.request.body.idempotency_key).toBeTruthy();
    refused.flush({ detail: { message: 'The recorded results no longer match.' } }, { status: 409, statusText: 'Conflict' });
    await screen.findByText('The recorded results no longer match.');
    expect((apply as HTMLButtonElement).disabled).toBe(true);
    expect(selected).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Revalidate saved configuration' }));
    const ready = { ...SAVED, status: 'ready', status_explanation: 'Saved results still match.' };
    http.expectOne(`${BASE}/${SAVED.id}/reprove`).flush(ready);
    await waitFor(() => http.expectOne(`${BASE}?limit=500`).flush([ready]));
    await screen.findByText('Saved results still match.');
    fireEvent.click(apply);
    expect(selected).toHaveBeenCalledWith(SAVED.id);
    http.verify();
  });

  it('reports unreadable saved settings and can retry without treating them as absent', async () => {
    const { http } = await setup();
    http.expectOne(`${BASE}?limit=500`).flush({ detail: { message: 'Research store unavailable.' } }, { status: 503, statusText: 'Unavailable' });
    await screen.findByText('Research store unavailable.');
    expect(screen.queryByText(/No Golden Search configurations/)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Read saved configurations again' }));
    await waitFor(() => http.expectOne(`${BASE}?limit=500`).flush([{ ...SAVED, status: 'revoked' }]));
    await screen.findByRole('option', { name: /revoked/ });
    fireEvent.change(screen.getByLabelText('Saved signal configuration'), { target: { value: SAVED.id } });
    expect((await screen.findByRole('button', { name: 'Use selected golden configuration' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole('button', { name: 'Revalidate saved configuration' })).toBeNull();
    http.verify();
  });
});
