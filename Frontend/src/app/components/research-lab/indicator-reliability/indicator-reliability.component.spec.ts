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
import { IndicatorReliabilityComponent } from './indicator-reliability.component';

describe('IndicatorReliabilityComponent (Indicator reliability study)', () => {
  let fixture: ComponentFixture<IndicatorReliabilityComponent>;
  let component: IndicatorReliabilityComponent;
  let http: HttpTestingController;

  const root = () => fixture.nativeElement as HTMLElement;
  const runs = () => http.match((request) => request.method === 'POST' && request.url.endsWith('/api/research/indicator-reliability'));

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
    const run = root().querySelector<HTMLButtonElement>('button.study-run');
    if (run === null) throw new Error('no Run study button');

    run.click();
    run.click();

    expect(runs()).toHaveLength(1);
  });
});
