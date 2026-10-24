import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { of } from 'rxjs';
import { render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { environment } from '../../../../environments/environment';
import { ValidateComponent } from './validate.component';

async function renderValidate(query: Record<string, string> = { ticker: 'SPY' }) {
  const paramMap = convertToParamMap(query);
  const result = await render(ValidateComponent, {
    providers: [
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: ActivatedRoute, useValue: { queryParamMap: of(paramMap), snapshot: { queryParamMap: paramMap } } },
    ],
  });
  const http = TestBed.inject(HttpTestingController);
  return { ...result, http, component: result.fixture.componentInstance };
}

describe('ValidateComponent', () => {
  it('runs disabled until both CSVs are selected', async () => {
    await renderValidate();
    const run = screen.getByRole('button', { name: 'Run validation' }) as HTMLButtonElement;
    expect(run.disabled).toBe(true);
  });

  it('uploads both files, renders the report, and offers download', async () => {
    const { http, component } = await renderValidate();
    component.ourCsvFile.set(new File(['t,close\n1,2\n'], 'ours.csv', { type: 'text/csv' }));
    component.tvCsvFile.set(new File(['time,close\n1,2\n'], 'theirs.csv', { type: 'text/csv' }));

    await userEvent.click(screen.getByRole('button', { name: 'Run validation' }));
    const req = await waitFor(() =>
      http.expectOne(`${environment.pythonServiceUrl}/api/dataset/validation-report`),
    );
    expect(req.request.method).toBe('POST');
    req.flush({ success: true, report: '# Comparison\n\npandas-ta matches TradingView on 250/250 bars.' });

    expect(await screen.findByText(/pandas-ta matches TradingView/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Download report' })).toBeTruthy();
    http.verify();
  });

  it('surfaces a failed validation response as an alert', async () => {
    const { http, component } = await renderValidate();
    component.ourCsvFile.set(new File(['a'], 'ours.csv'));
    component.tvCsvFile.set(new File(['b'], 'theirs.csv'));

    await userEvent.click(screen.getByRole('button', { name: 'Run validation' }));
    const req = await waitFor(() =>
      http.expectOne(`${environment.pythonServiceUrl}/api/dataset/validation-report`),
    );
    req.flush({ success: false, report: '' }, { status: 200, statusText: 'OK' });

    expect(await screen.findByRole('alert')).toBeTruthy();
    http.verify();
  });
});
