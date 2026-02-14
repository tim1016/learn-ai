import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { TickersComponent } from './tickers.component';
import { TickerService } from '../../services/ticker.service';
import { createMockTicker } from '../../../testing/factories/market-data.factory';

describe('TickersComponent', () => {
  let component: TickersComponent;
  let fixture: ComponentFixture<TickersComponent>;
  let tickerServiceMock: jest.Mocked<Pick<TickerService, 'getTickers' | 'getAggregateStats'>>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    tickerServiceMock = {
      getTickers: jest.fn().mockReturnValue(of([])),
      getAggregateStats: jest.fn().mockReturnValue(of({ count: 0, earliest: null, latest: null })),
    };

    await TestBed.configureTestingModule({
      imports: [TickersComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: TickerService, useValue: tickerServiceMock },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(TickersComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should set loading on init', () => {
    fixture.detectChanges();
    // After init with empty response, loading becomes false
    expect(component.loading).toBe(false);
    expect(component.tickers).toEqual([]);
  });

  it('should load tickers from service', () => {
    const tickers = [
      createMockTicker({ id: 1, symbol: 'AAPL' }),
      createMockTicker({ id: 2, symbol: 'MSFT' }),
    ];
    tickerServiceMock.getTickers.mockReturnValue(of(tickers));

    fixture.detectChanges();

    expect(component.tickers.length).toBe(2);
    expect(component.tickers[0].symbol).toBe('AAPL');
  });

  it('should handle error from service', () => {
    tickerServiceMock.getTickers.mockReturnValue(throwError(() => new Error('Network error')));

    fixture.detectChanges();

    expect(component.loading).toBe(false);
    expect(component.error).toBe('Network error');
  });

  it('should map XNAS to NASDAQ', () => {
    const ticker = createMockTicker({ primaryExchange: 'XNAS' });
    expect(component.getExchange(ticker as any)).toBe('NASDAQ');
  });

  it('should map XNYS to NYSE', () => {
    const ticker = createMockTicker({ primaryExchange: 'XNYS' });
    expect(component.getExchange(ticker as any)).toBe('NYSE');
  });

  it('should map XASE to AMEX', () => {
    const ticker = createMockTicker({ primaryExchange: 'XASE' });
    expect(component.getExchange(ticker as any)).toBe('AMEX');
  });

  it('should default to NASDAQ for unknown exchange', () => {
    const ticker = createMockTicker({ primaryExchange: 'UNKNOWN' });
    expect(component.getExchange(ticker as any)).toBe('NASDAQ');
  });

  it('should default to NASDAQ when primaryExchange is null', () => {
    const ticker = createMockTicker({ primaryExchange: null });
    expect(component.getExchange(ticker as any)).toBe('NASDAQ');
  });
});
