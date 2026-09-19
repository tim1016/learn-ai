import { ComponentFixture, TestBed } from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
  type TestRequest,
} from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { vi } from 'vitest';

import { environment } from '../../../../environments/environment';
import {
  fakeTickerCatalog,
  provideFakeTickerCatalog,
} from '../../../shared/ticker-catalog/testing/fake-ticker-catalog';
import { JobsService, type JobState } from '../../../services/jobs.service';
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

  beforeEach(async () => {
    const jobs = signal<JobState[]>([]);
    await TestBed.configureTestingModule({
      imports: [SignalRunnerComponent, HttpClientTestingModule],
      providers: [
        {
          provide: JobsService,
          useValue: {
            jobs,
            job: () => undefined,
            startJob: vi.fn(),
            cancelJob: vi.fn(),
            fetchResult: vi.fn(),
            dismiss: vi.fn(),
          },
        },
        // The ticker card reads the lake catalog on init; without this the
        // real service fires a doomed XHR on every run.
        provideFakeTickerCatalog(
          fakeTickerCatalog([{ symbol: 'SPY', name: 'SPDR S&P 500 ETF Trust', exchange: 'ARCA' }]),
        ),
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
});
