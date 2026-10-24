import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { environment } from '../../../../environments/environment';
import { QualityEvidenceComponent } from './quality-evidence.component';

const URL = `${environment.pythonServiceUrl}/api/data-quality/analyze`;

async function renderEvidence() {
  const result = await render(QualityEvidenceComponent, {
    providers: [provideHttpClient(), provideHttpClientTesting()],
    inputs: { symbol: 'SPY', from: '2026-04-15', to: '2026-05-15' },
  });
  return { ...result, http: TestBed.inject(HttpTestingController) };
}

describe('QualityEvidenceComponent', () => {
  it('asks for nothing until it is asked, then runs on the committed window and renders the server numbers', async () => {
    const { http } = await renderEvidence();
    http.expectNone(URL);

    await userEvent.click(screen.getByText('Quality evidence: raw against clean bars'));
    await userEvent.click(screen.getByRole('button', { name: 'Run quality analysis' }));
    const req = await waitFor(() => http.expectOne(URL));
    expect(req.request.body['symbol']).toBe('SPY');
    expect(req.request.body['from_date']).toBe('2026-04-15');
    expect(req.request.body['to_date']).toBe('2026-05-15');
    req.flush({
      success: true,
      ticker: 'SPY',
      from_date: '2026-04-15',
      to_date: '2026-05-15',
      raw_summary: {
        total_bars: 100, trading_days: 20, zero_volume_bars: 2, flat_bars_ohlc_equal: 0,
        fractional_volume_bars: 0, vwap_above_high: 0, vwap_below_low: 0, ohlc_violations: 0,
        duplicate_timestamps: 1, weekend_bars: 0, intraday_gaps: 3,
      },
      clean_summary: {
        total_bars: 99, trading_days: 20, zero_volume_bars: 0, flat_bars_ohlc_equal: 0,
        fractional_volume_bars: 0, vwap_above_high: 0, vwap_below_low: 0, ohlc_violations: 0,
        duplicate_timestamps: 0, weekend_bars: 0, intraday_gaps: 0,
      },
      steps: [],
    });

    expect(await screen.findByText('Total bars')).toBeTruthy();
    expect(await screen.findByText('99')).toBeTruthy();
    http.verify();
  });

  it('says why when the analysis fails, and shows no numbers', async () => {
    const { http } = await renderEvidence();

    await userEvent.click(screen.getByRole('button', { name: 'Run quality analysis' }));
    const req = await waitFor(() => http.expectOne(URL));
    req.flush({ detail: 'provider unavailable' }, { status: 503, statusText: 'Service Unavailable' });

    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.queryByText('Total bars')).toBeNull();
    http.verify();
  });
});
