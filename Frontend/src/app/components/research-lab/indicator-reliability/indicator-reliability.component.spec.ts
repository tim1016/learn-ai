import { vi } from 'vitest';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideRouter } from '@angular/router';

import { provideFakeDateWorld } from '../../../shared/date-picker/testing/fake-trading-calendar';
import {
  fakeTickerCatalog,
  provideFakeTickerCatalog,
} from '../../../shared/ticker-catalog/testing/fake-ticker-catalog';
import { fakeVendorCatalog, provideFakeVendorCatalog } from '../../../shared/symbol-catalog/testing/fake-symbol-catalog';
import { environment } from '../../../../environments/environment';
import { RECORD_ID, storedRecord } from '../../../research/stored-study/testing/stored-study-fixtures';
import { IndicatorReliabilityComponent } from './indicator-reliability.component';

describe('IndicatorReliabilityComponent (Indicator reliability study)', () => {
  let fixture: ComponentFixture<IndicatorReliabilityComponent>;
  let component: IndicatorReliabilityComponent;
  let http: HttpTestingController;

  const root = () => fixture.nativeElement as HTMLElement;
  const runs = () => http.match((request) => request.method === 'POST' && request.url.endsWith('/api/research/indicator-reliability'));
  const paramReads = () => http.match((request) => request.url.includes('/api/research/indicator-params/'));
  const SCHEMA = [{ name: 'length', type: 'int', default: 14, min: 1, max: 200, description: 'Lookback.' }];
  /** The indicator's parameters arrive; until they do, Run study is off. */
  const answerParams = () => {
    for (const request of paramReads()) request.flush(SCHEMA);
    fixture.detectChanges();
  };
  const runButton = () => root().querySelector<HTMLButtonElement>('button.study-run');

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [IndicatorReliabilityComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        provideFakeTickerCatalog(
          fakeTickerCatalog([{ symbol: 'SPY', name: 'SPDR S&P 500 ETF Trust', exchange: 'ARCA' }]),
        ),
        provideFakeVendorCatalog(fakeVendorCatalog([])),
        ...provideFakeDateWorld(),
      ],
    }).compileComponents();

    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(IndicatorReliabilityComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('says its session and bars are fixed instead of offering controls for them', () => {
    expect.soft(root().querySelector('app-sampling-card'), 'no resolution or session control').toBeNull();
    const fixed = root().querySelector('[data-testid="study-constraints"]')?.textContent?.replace(/\s+/g, ' ') ?? '';
    expect.soft(fixed).toContain('regular trading hours, fixed');
    expect.soft(fixed).toContain('1 minute, fixed');
    // The default window is long enough for the picker's hour-bars hint.
    expect.soft(root().textContent, 'no hint to switch to bars the study does not read').not.toContain('Switch to hour');
  });

  it('starts nothing when an input changes', () => {
    component.range.update((r) => ({ ...r, symbol: 'MSFT', from: '2024-02-01' }));
    component.indicatorName.set('macd');
    component.selectedHorizons.set([1, 5]);
    component.includeSlope.set(true);
    fixture.detectChanges();

    expect(runs()).toHaveLength(0);
  });

  it('sends one request when Run study is pressed twice before the page re-renders', () => {
    answerParams();
    const run = runButton();
    if (run === null) throw new Error('no Run study button');

    run.click();
    run.click();

    expect(runs()).toHaveLength(1);
  });

  it('keeps Run study off until the indicator’s parameters have arrived', () => {
    expect(runButton()?.disabled, 'the parameters are still being read').toBe(true);
    expect(root().querySelector('.study-run__summary')?.textContent).toContain('Waiting for the indicator’s parameters');

    answerParams();

    expect(runButton()?.disabled).toBe(false);
  });

  it('shows a failed parameter read in step 2, keeps Run study off, and runs again once a retry succeeds', () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    for (const request of paramReads()) request.flush({ detail: 'schema unavailable' }, { status: 500, statusText: 'Server Error' });
    fixture.detectChanges();

    const problem = root().querySelector('[data-testid="params-error"]');
    expect.soft(problem?.textContent).toContain('The parameters of RSI could not be loaded: schema unavailable.');
    expect.soft(runButton()?.disabled).toBe(true);
    expect.soft(root().querySelector('.study-run__summary')?.textContent).toContain('use Try again in step 2');

    problem?.querySelector<HTMLButtonElement>('button.config-retry')?.click();
    answerParams();

    expect.soft(root().querySelector('[data-testid="params-error"]')).toBeNull();
    expect.soft(runButton()?.disabled).toBe(false);
    expect(errors, 'a failure is shown to the reader, not written to the console').not.toHaveBeenCalled();
  });

  it('shows a failed indicator list in step 2 instead of leaving the picker silently empty', () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    http
      .expectOne(`${environment.pythonServiceUrl}/api/research/indicators`)
      .flush({}, { status: 503, statusText: 'Unavailable' });
    fixture.detectChanges();

    expect(root().querySelector('[data-testid="indicators-error"]')?.textContent).toContain(
      'The indicator list could not be loaded: the data plane answered 503.',
    );
    expect(errors).not.toHaveBeenCalled();
  });

  it('fills the inputs from a saved run, keeps its parameters over the indicator’s defaults, starts nothing, then names the run when Run study is pressed', async () => {
    fixture.componentRef.setInput('rerun', RECORD_ID);
    fixture.detectChanges();
    http
      .expectOne(`${environment.pythonServiceUrl}/api/research/study-records/indicator-reliability/${RECORD_ID}`)
      .flush(storedRecord('indicator-reliability'));
    await new Promise((resolve) => setTimeout(resolve)); // the read resolves in a later task than the flush
    await fixture.whenStable();
    // The indicator's own parameters arrive after the fill; the saved ones stay.
    for (const request of http.match((r) => r.url.includes('/api/research/indicator-params/'))) {
      request.flush([{ name: 'length', type: 'int', default: 10, min: 1, max: 200, description: 'Lookback.' }]);
    }
    await fixture.whenStable();

    expect(component.range()).toMatchObject({ symbol: 'SPY', from: '2024-01-02', to: '2024-03-28' });
    expect(component.indicatorName()).toBe('rsi');
    expect(component.paramValues()).toEqual({ length: 14 });
    expect(component.selectedHorizons()).toEqual([5, 1]);
    expect(runs()).toHaveLength(0);

    component.runAnalysis();

    const [run] = runs();
    expect(run.request.body).toMatchObject({ indicator_name: 'rsi', horizons: [5, 1], rerun_of: RECORD_ID });
  });
});
