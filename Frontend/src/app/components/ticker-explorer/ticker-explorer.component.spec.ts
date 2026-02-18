import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { TickerExplorerComponent } from './ticker-explorer.component';

describe('TickerExplorerComponent', () => {
  let component: TickerExplorerComponent;
  let fixture: ComponentFixture<TickerExplorerComponent>;
  let httpMock: HttpTestingController;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [TickerExplorerComponent],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();

    fixture = TestBed.createComponent(TickerExplorerComponent);
    component = fixture.componentInstance;
    httpMock = TestBed.inject(HttpTestingController);
    fixture.detectChanges();
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should have default ticker AAPL', () => {
    expect(component.ticker()).toBe('AAPL');
  });

  it('should not be loading initially', () => {
    expect(component.loading()).toBe(false);
  });

  it('should have no contracts initially', () => {
    expect(component.allContracts()).toEqual([]);
    expect(component.strikes()).toEqual([]);
  });

  it('should have null underlying initially', () => {
    expect(component.underlying()).toBeNull();
  });

  it('should compute empty expiration dates when no contracts', () => {
    expect(component.expirationDates()).toEqual([]);
  });

  it('should compute call and put contracts separately', () => {
    component.allContracts.set([
      { ticker: 'O:AAPL250221C00185000', contractType: 'call', strikePrice: 185, expirationDate: '2025-02-21', breakEvenPrice: null, impliedVolatility: null, openInterest: null, greeks: null, day: null },
      { ticker: 'O:AAPL250221P00185000', contractType: 'put', strikePrice: 185, expirationDate: '2025-02-21', breakEvenPrice: null, impliedVolatility: null, openInterest: null, greeks: null, day: null },
      { ticker: 'O:AAPL250221C00190000', contractType: 'call', strikePrice: 190, expirationDate: '2025-02-21', breakEvenPrice: null, impliedVolatility: null, openInterest: null, greeks: null, day: null },
    ]);

    expect(component.callContracts().length).toBe(2);
    expect(component.putContracts().length).toBe(1);
    expect(component.strikes()).toEqual([185, 190]);
  });

  it('should compute ATM strike closest to underlying price', () => {
    component.underlying.set({ ticker: 'AAPL', price: 187, change: 0, changePercent: 0 });
    component.allContracts.set([
      { ticker: 'C1', contractType: 'call', strikePrice: 185, expirationDate: '2025-02-21', breakEvenPrice: null, impliedVolatility: null, openInterest: null, greeks: null, day: null },
      { ticker: 'C2', contractType: 'call', strikePrice: 190, expirationDate: '2025-02-21', breakEvenPrice: null, impliedVolatility: null, openInterest: null, greeks: null, day: null },
    ]);

    expect(component.atmStrike()).toBe(185);
  });

  it('should filter by expiration date', () => {
    component.allContracts.set([
      { ticker: 'C1', contractType: 'call', strikePrice: 185, expirationDate: '2025-02-21', breakEvenPrice: null, impliedVolatility: null, openInterest: null, greeks: null, day: null },
      { ticker: 'C2', contractType: 'call', strikePrice: 190, expirationDate: '2025-02-28', breakEvenPrice: null, impliedVolatility: null, openInterest: null, greeks: null, day: null },
    ]);

    expect(component.filteredContracts().length).toBe(2);

    component.selectedExpiration.set('2025-02-21');
    expect(component.filteredContracts().length).toBe(1);
    expect(component.filteredContracts()[0].strikePrice).toBe(185);
  });

  it('should format IV as percentage', () => {
    expect(component.formatIv(0.25)).toBe('25.0%');
    expect(component.formatIv(null)).toBe('—');
  });

  it('should identify ITM calls correctly', () => {
    component.underlying.set({ ticker: 'AAPL', price: 187, change: 0, changePercent: 0 });
    expect(component.isItm(185, 'call')).toBe(true);
    expect(component.isItm(190, 'call')).toBe(false);
  });

  it('should identify ITM puts correctly', () => {
    component.underlying.set({ ticker: 'AAPL', price: 187, change: 0, changePercent: 0 });
    expect(component.isItm(190, 'put')).toBe(true);
    expect(component.isItm(185, 'put')).toBe(false);
  });

  it('should fetch snapshot and populate signals', async () => {
    const promise = component.fetchSnapshot();

    const req = httpMock.expectOne('http://localhost:5000/graphql');
    expect(req.request.method).toBe('POST');
    req.flush({
      data: {
        getOptionsChainSnapshot: {
          success: true,
          underlying: { ticker: 'AAPL', price: 185.5, change: 2.3, changePercent: 1.25 },
          contracts: [
            { ticker: 'O:AAPL250221C00185000', contractType: 'call', strikePrice: 185, expirationDate: '2025-02-21', breakEvenPrice: 187.5, impliedVolatility: 0.25, openInterest: 1500, greeks: { delta: 0.52, gamma: 0.03, theta: -0.15, vega: 0.2 }, day: { open: 3, high: 3.5, low: 2.8, close: 3.2, volume: 5000, vwap: 3.1 } },
          ],
          count: 1,
          error: null,
        }
      }
    });

    await promise;

    expect(component.underlying()?.ticker).toBe('AAPL');
    expect(component.underlying()?.price).toBe(185.5);
    expect(component.allContracts().length).toBe(1);
    expect(component.loading()).toBe(false);
    expect(component.error()).toBeNull();
  });

  it('should handle fetch error', async () => {
    const promise = component.fetchSnapshot();

    const req = httpMock.expectOne('http://localhost:5000/graphql');
    req.error(new ProgressEvent('error'));

    await promise;

    expect(component.loading()).toBe(false);
    expect(component.error()).toBeTruthy();
  });
});
