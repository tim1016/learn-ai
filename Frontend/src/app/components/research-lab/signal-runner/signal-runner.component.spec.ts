import { provideFakeDateWorld } from '../../../shared/date-picker/testing/fake-trading-calendar';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
  type TestRequest,
} from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { vi } from 'vitest';
import { provideRouter } from '@angular/router';

import { environment } from '../../../../environments/environment';
import {
  fakeTickerCatalog,
  provideFakeTickerCatalog,
} from '../../../shared/ticker-catalog/testing/fake-ticker-catalog';
import {
  fakeVendorCatalog,
  provideFakeVendorCatalog,
} from '../../../shared/symbol-catalog/testing/fake-symbol-catalog';

import { JobsService, type JobState } from '../../../services/jobs.service';
import { RECORD_ID, storedRecord } from '../../../research/stored-study/testing/stored-study-fixtures';
import { SignalRunnerComponent } from './signal-runner.component';

/** A one-indicator `/api/dataset/available` catalog. */
const CATALOG = {
  momentum: [
    {
      name: 'mom',
      category: 'momentum',
      description: 'Momentum.',
      configurable_params: [
        { name: 'length', type: 'int', default: 10, min: 1, max: 200, description: 'Lookback.' },
      ],
    },
  ],
};

describe('SignalRunnerComponent indicator catalog load', () => {
  const catalogUrl = `${environment.pythonServiceUrl}/api/dataset/available`;
  let fixture: ComponentFixture<SignalRunnerComponent>;
  let startJob: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    const jobs = signal<JobState[]>([]);
    startJob = vi.fn().mockResolvedValue('job-id-1');
    await TestBed.configureTestingModule({
      imports: [SignalRunnerComponent, HttpClientTestingModule],
      providers: [
        {
          provide: JobsService,
          useValue: {
            jobs,
            job: () => undefined,
            startJob,
            cancelJob: vi.fn(),
            fetchResult: vi.fn(),
            dismiss: vi.fn(),
          },
        },
        provideRouter([]),
        // The ticker card reads the lake catalog on init; without this the
        // real service fires a doomed XHR on every run.
        provideFakeTickerCatalog(
          fakeTickerCatalog([{ symbol: 'SPY', name: 'SPDR S&P 500 ETF Trust', exchange: 'ARCA' }]),
        ),
        // The joined picker universe also reads the vendor catalog; this spec
        // drives every request through HttpTestingController, so stub it to
        // keep `whenStable()` from hanging on an unflushed request.
        provideFakeVendorCatalog(fakeVendorCatalog([])),
        ...provideFakeDateWorld(),
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(SignalRunnerComponent);
    fixture.detectChanges();
  });

  async function settleCatalog(respond: (req: TestRequest) => void): Promise<HTMLElement> {
    respond(TestBed.inject(HttpTestingController).expectOne(catalogUrl));
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('says so in the picker when the catalog fails to load', async () => {
    const el = await settleCatalog((req) =>
      req.flush('boom', { status: 500, statusText: 'Internal Server Error' }),
    );

    const picker = el.querySelector('app-indicator-picker');
    expect(picker?.querySelector('[role="alert"]')?.textContent).toContain(
      'Indicators could not be loaded.',
    );
    expect(picker?.textContent).not.toContain('No indicators available');
    // Fixed copy: the service's raw HTTP error never reaches the page.
    expect(picker?.textContent).not.toContain('Http failure response');
  });

  it('shows no failure when the catalog loads', async () => {
    const el = await settleCatalog((req) =>
      req.flush({ success: true, categories: CATALOG, total: 1 }),
    );

    const picker = el.querySelector('app-indicator-picker');
    expect(picker?.querySelector('[role="alert"]')).toBeNull();
    expect(picker?.textContent).not.toContain('Indicators could not be loaded.');
    expect(picker?.textContent).toContain('1 of 1');
  });

  describe('rerun from a saved run', () => {
    it('fills the inputs, flip sign and regime gate from the saved run, starts nothing, then names it when Run study is pressed', async () => {
      const component = fixture.componentInstance;
      fixture.componentRef.setInput('rerun', RECORD_ID);
      fixture.detectChanges();
      TestBed.inject(HttpTestingController)
        .expectOne(`${environment.pythonServiceUrl}/api/research/study-records/signal-study/${RECORD_ID}`)
        .flush(storedRecord('signal-study'));
      await new Promise((resolve) => setTimeout(resolve)); // the read resolves in a later task than the flush
      await fixture.whenStable();

      expect(component.range()).toEqual({ symbol: 'SPY', from: '2024-01-02', to: '2024-03-28', resolution: 'minute', multiplier: 15 });
      expect(component.selectedIndicator()).toBe('macd');
      expect(component.selectedParams()).toEqual({ fast: 12, slow: 26, signal: 9 });
      expect(component.flipSign()).toBe(false);
      expect(component.regimeGateEnabled()).toBe(true);
      expect(startJob).not.toHaveBeenCalled();

      await component.runSignalEngine();

      expect(startJob.mock.calls[0][1]).toMatchObject({ feature_name: 'macd_signal', flip_sign: false, rerun_of: RECORD_ID });
    });
  });

  describe('study layout', () => {
    const root = () => fixture.nativeElement as HTMLElement;

    it('offers only the bars the 15-bar target can use, and no session control', () => {
      const resolutions = Array.from(root().querySelectorAll('.res-btn')).map((el) => el.textContent?.trim());
      const multipliers = Array.from(
        root().querySelectorAll('select[aria-label="Bar multiplier"] option'),
      ).map((el) => el.textContent?.trim());

      expect.soft(resolutions, 'hour and daily bars always fail').toEqual(['1m']);
      expect.soft(multipliers, 'bars of 30 minutes or longer always fail').toEqual(['1×', '5×', '15×']);
      expect.soft(root().querySelector('.session'), 'the session is not a control').toBeNull();
      expect.soft(root().querySelector('[data-testid="study-constraints"]')?.textContent).toContain(
        'extended hours included, fixed',
      );
      // The default window is long enough for the picker's hour-bars hint.
      expect.soft(root().textContent, 'no hint to switch to bars that always fail').not.toContain('Switch to hour');
    });

    it('starts nothing when an input changes', () => {
      const component = fixture.componentInstance;
      component.range.update((r) => ({ ...r, symbol: 'MSFT' }));
      component.flipSign.set(false);
      component.regimeGateEnabled.set(false);
      fixture.detectChanges();

      expect(startJob).not.toHaveBeenCalled();
    });

    it('starts one job when Run study is pressed twice before the page re-renders', async () => {
      const run = root().querySelector<HTMLButtonElement>('button.study-run');
      if (run === null) throw new Error('no Run study button');

      run.click();
      run.click();
      await fixture.whenStable();

      expect(startJob).toHaveBeenCalledTimes(1);
    });
  });
});
