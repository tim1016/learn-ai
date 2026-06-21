import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { afterEach, describe, expect, it } from 'vitest';
import { IncidentsPanelComponent } from './incidents-panel.component';
import type { IncidentRow } from './incidents.types';

const RUN_ID = 'run-abc';

function makeRow(overrides: Partial<IncidentRow> = {}): IncidentRow {
  return {
    ts_ms: 1781014378021,
    raw_ts: '2026-06-09 14:12:58.021',
    level: 'ERROR',
    logger: 'app.broker.ibkr.client',
    message: 'something happened',
    traceback: null,
    incident_category: 'unknown',
    ...overrides,
  };
}

function render(): {
  fixture: ComponentFixture<IncidentsPanelComponent>;
  httpMock: HttpTestingController;
} {
  TestBed.configureTestingModule({
    providers: [provideHttpClient(), provideHttpClientTesting()],
  });
  const fixture = TestBed.createComponent(IncidentsPanelComponent);
  fixture.componentRef.setInput('runId', RUN_ID);
  fixture.detectChanges();
  const httpMock = TestBed.inject(HttpTestingController);
  return { fixture, httpMock };
}

function flushIncidents(httpMock: HttpTestingController, rows: IncidentRow[]): void {
  const req = httpMock.expectOne(`/api/live-runs/${RUN_ID}/incidents`);
  expect(req.request.method).toBe('GET');
  req.flush(rows);
}

afterEach(() => TestBed.resetTestingModule());

describe('IncidentsPanelComponent', () => {
  it('renders the operator-language title for a broker disconnect rather than raw "Error 1100"', async () => {
    // Backend emits the WARNING-level header + classified category; the
    // panel must surface the trader copy, not the ib_async wrapper string.
    const { fixture, httpMock } = render();
    flushIncidents(httpMock, [
      makeRow({
        level: 'WARNING',
        logger: 'ib_async.wrapper',
        message: 'Error 1100, reqId -1: lost',
        incident_category: 'broker_disconnect',
      }),
    ]);
    await fixture.whenStable();
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Recent Incidents');
    expect(text).toContain('Broker connection lost');
    // The raw ib_async string is only visible if the user opens the row's
    // traceback details — never in the default surface.
    expect(text).not.toContain('Error 1100, reqId -1');
  });

  it('applies severity tone classes per row', async () => {
    const { fixture, httpMock } = render();
    flushIncidents(httpMock, [
      makeRow({ incident_category: 'broker_disconnect' }), // warning
      makeRow({ incident_category: 'engine_fatal' }), // critical
      makeRow({ incident_category: 'lost_fill' }), // blocking
    ]);
    await fixture.whenStable();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('.incident-row.tone-warning')).toBeTruthy();
    expect(el.querySelector('.incident-row.tone-critical')).toBeTruthy();
    expect(el.querySelector('.incident-row.tone-blocking')).toBeTruthy();
  });

  it('falls back to UNKNOWN copy when the backend omits incident_category', async () => {
    // Rollout safety: an older or out-of-band backend may emit rows with
    // an empty / missing category. The panel must not crash; it renders
    // UNKNOWN copy and keeps the raw traceback accessible.
    const { fixture, httpMock } = render();
    flushIncidents(httpMock, [
      // Cast lets us simulate a backend that didn't fill the field.
      makeRow({
        incident_category: '' as unknown as IncidentRow['incident_category'],
        traceback: 'Traceback (most recent call last):\n  RuntimeError: ...',
      }),
    ]);
    await fixture.whenStable();
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Unknown error');
  });

  it('reveals the recommended action and traceback details only after the row is expanded', async () => {
    const { fixture, httpMock } = render();
    flushIncidents(httpMock, [
      makeRow({
        incident_category: 'engine_fatal',
        traceback: 'Traceback (most recent call last):\n  RuntimeError: boom',
      }),
    ]);
    await fixture.whenStable();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    // Collapsed by default — body content not yet rendered.
    expect(el.querySelector('.incident-body')).toBeNull();

    const head = el.querySelector<HTMLButtonElement>('.incident-head');
    if (!head) throw new Error('incident-head not found');
    head.click();
    fixture.detectChanges();

    const body = el.querySelector('.incident-body');
    expect(body).toBeTruthy();
    expect(body?.textContent).toContain('Recommended:');
    expect(body?.textContent).toContain('Original traceback');
  });

  it('emits rawLogRequested when the "View raw log" button is clicked', async () => {
    const { fixture, httpMock } = render();
    flushIncidents(httpMock, [makeRow({ incident_category: 'engine_fatal' })]);
    await fixture.whenStable();
    fixture.detectChanges();

    let emissions = 0;
    fixture.componentInstance.rawLogRequested.subscribe(() => emissions++);

    const el = fixture.nativeElement as HTMLElement;
    const head = el.querySelector<HTMLButtonElement>('.incident-head');
    const rawButton = (): HTMLButtonElement | null =>
      el.querySelector<HTMLButtonElement>('.raw-log-button');
    if (!head) throw new Error('incident-head not found');
    head.click();
    fixture.detectChanges();
    const raw = rawButton();
    if (!raw) throw new Error('raw-log-button not found after expand');
    raw.click();

    expect(emissions).toBe(1);
  });

  it('shows a "no recent incidents" affordance when the run has none', async () => {
    const { fixture, httpMock } = render();
    flushIncidents(httpMock, []);
    await fixture.whenStable();
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('No warnings or errors for this run.');
  });
});
