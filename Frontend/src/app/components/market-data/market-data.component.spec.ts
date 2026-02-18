import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ActivatedRoute } from '@angular/router';
import { MarketDataComponent } from './market-data.component';
import { createMockAggregate, createMockSummary } from '../../../testing/factories/market-data.factory';

describe('MarketDataComponent', () => {
  let component: MarketDataComponent;
  let fixture: ComponentFixture<MarketDataComponent>;
  let httpMock: HttpTestingController;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [MarketDataComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ActivatedRoute, useValue: { snapshot: { queryParams: {} } } },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(MarketDataComponent);
    component = fixture.componentInstance;
    httpMock = TestBed.inject(HttpTestingController);
    fixture.detectChanges();

    // Flush the holidays request triggered by ngOnInit
    flushHolidaysRequest();
  });

  afterEach(() => httpMock.verify());

  function flushHolidaysRequest() {
    const req = httpMock.match(
      r => r.url.includes('/api/market/holidays')
    );
    req.forEach(r => r.flush({ success: true, events: [], count: 0, error: null }));
  }

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should initialize date range on ngOnInit', () => {
    fixture.detectChanges();
    const today = new Date().toISOString().split('T')[0];
    expect(component.toDate()).toBe(today);
    expect(component.fromDate()).toBeTruthy();
    // fromDate should be ~3 months before today
    const from = new Date(component.fromDate());
    const to = new Date(component.toDate());
    const diffMonths = (to.getFullYear() - from.getFullYear()) * 12 + (to.getMonth() - from.getMonth());
    expect(diffMonths).toBe(3);
  });

  it('should set error when ticker is empty', () => {
    component.ticker.set('');
    component.fetchData();
    expect(component.error()).toBe('Please enter a ticker symbol');
    expect(component.loading()).toBe(false);
  });

  it('should set loading and clear state when fetching', () => {
    component.ticker.set('AAPL');
    component.fetchData();
    expect(component.loading()).toBe(true);
    expect(component.error()).toBeNull();
    expect(component.aggregates()).toEqual([]);
    expect(component.summary()).toBeNull();

    // consume pending request
    httpMock.expectOne('http://localhost:5000/graphql');
  });

  it('should load all aggregates on successful fetch', () => {
    component.ticker.set('AAPL');
    component.fromDate.set('2026-01-01');
    component.toDate.set('2026-01-31');
    component.fetchData();

    const req = httpMock.expectOne('http://localhost:5000/graphql');
    req.flush({
      data: {
        getOrFetchStockAggregates: {
          ticker: 'AAPL',
          aggregates: [
            createMockAggregate({ id: 1, timestamp: '2026-01-15T00:00:00Z' }),
            createMockAggregate({ id: 2, timestamp: '2026-01-05T00:00:00Z' }),
            createMockAggregate({ id: 3, timestamp: '2026-01-10T00:00:00Z' }),
          ],
          summary: createMockSummary(),
        },
      },
    });

    expect(component.loading()).toBe(false);
    expect(component.aggregates().length).toBe(3);
    const timestamps = component.aggregates().map(a => a.timestamp);
    expect(timestamps).toContain('2026-01-05T00:00:00Z');
    expect(timestamps).toContain('2026-01-10T00:00:00Z');
    expect(timestamps).toContain('2026-01-15T00:00:00Z');
  });

  it('should set summary from response', () => {
    component.ticker.set('AAPL');
    component.fetchData();

    const summary = createMockSummary({ periodHigh: 250 });
    const req = httpMock.expectOne('http://localhost:5000/graphql');
    req.flush({
      data: {
        getOrFetchStockAggregates: {
          ticker: 'AAPL',
          aggregates: [createMockAggregate()],
          summary,
        },
      },
    });

    expect(component.summary()).toEqual(summary);
  });

  it('should handle errors', () => {
    component.ticker.set('AAPL');
    component.fetchData();

    const req = httpMock.expectOne('http://localhost:5000/graphql');
    req.error(new ProgressEvent('error'), { status: 500, statusText: 'Server Error' });

    expect(component.loading()).toBe(false);
    expect(component.error()).toBeTruthy();
  });
});
