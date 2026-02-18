import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { vi } from 'vitest';
import { OptionsHistoryComponent, ContractRow, ScanResult } from './options-history.component';

vi.mock('lightweight-charts', () => {
  const mockTimeScale = { fitContent: vi.fn() };
  const createMockSeries = () => ({ setData: vi.fn(), applyOptions: vi.fn() });
  const createMockChart = () => ({
    addSeries: vi.fn().mockReturnValue(createMockSeries()),
    removeSeries: vi.fn(),
    timeScale: vi.fn().mockReturnValue(mockTimeScale),
    applyOptions: vi.fn(),
    remove: vi.fn(),
  });
  return {
    createChart: vi.fn().mockImplementation(() => createMockChart()),
    CandlestickSeries: 'CandlestickSeries',
    LineSeries: 'LineSeries',
    HistogramSeries: 'HistogramSeries',
  };
});

describe('OptionsHistoryComponent', () => {
  let component: OptionsHistoryComponent;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [OptionsHistoryComponent],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    const fixture = TestBed.createComponent(OptionsHistoryComponent);
    component = fixture.componentInstance;
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  describe('initialization', () => {
    it('should create the component', () => {
      expect(component).toBeTruthy();
    });

    it('should default ticker to AAPL', () => {
      expect(component.ticker()).toBe('AAPL');
    });

    it('should default atmMethod to open', () => {
      expect(component.atmMethod()).toBe('open');
    });

    it('should default numStrikes to 5', () => {
      expect(component.numStrikes()).toBe(5);
    });

    it('should not be loading initially', () => {
      expect(component.loading()).toBe(false);
    });

    it('should have no error initially', () => {
      expect(component.error()).toBeNull();
    });

    it('should set analysisDate to a weekday', () => {
      const date = new Date(component.analysisDate() + 'T00:00:00');
      const day = date.getDay();
      expect(day).not.toBe(0); // not Sunday
      expect(day).not.toBe(6); // not Saturday
    });
  });

  describe('formatPrice', () => {
    it('should format numbers to 2 decimal places', () => {
      expect(component.formatPrice(150.123)).toBe('150.12');
    });

    it('should return -- for null', () => {
      expect(component.formatPrice(null)).toBe('--');
    });

    it('should return -- for undefined', () => {
      expect(component.formatPrice(undefined)).toBe('--');
    });
  });

  describe('formatChange', () => {
    it('should show + sign for positive values', () => {
      expect(component.formatChange(5.5)).toBe('+5.50');
    });

    it('should show - sign for negative values', () => {
      expect(component.formatChange(-3.2)).toBe('-3.20');
    });

    it('should return -- for null', () => {
      expect(component.formatChange(null)).toBe('--');
    });
  });

  describe('formatChangePct', () => {
    it('should show percentage with sign', () => {
      expect(component.formatChangePct(12.5)).toBe('+12.5%');
    });

    it('should show negative percentage', () => {
      expect(component.formatChangePct(-3.1)).toBe('-3.1%');
    });

    it('should return -- for null', () => {
      expect(component.formatChangePct(null)).toBe('--');
    });
  });

  describe('formatVolume', () => {
    it('should format with locale separators', () => {
      const result = component.formatVolume(1000000);
      expect(result).toContain('1');
      expect(result).toContain('000');
    });

    it('should return -- for null', () => {
      expect(component.formatVolume(null)).toBe('--');
    });

    it('should return -- for undefined', () => {
      expect(component.formatVolume(undefined)).toBe('--');
    });
  });

  describe('computed: callRows and putRows', () => {
    it('should separate calls and puts from contractRows', () => {
      const rows: ContractRow[] = [
        { optionTicker: 'O:AAPL260220C00230000', contractType: 'call', strikePrice: 230, dailyBar: null, prevDayClose: null, changeFromPrevClose: null, changePercent: null, isAtm: false, relativeStrike: 0 },
        { optionTicker: 'O:AAPL260220P00230000', contractType: 'put', strikePrice: 230, dailyBar: null, prevDayClose: null, changeFromPrevClose: null, changePercent: null, isAtm: false, relativeStrike: 0 },
        { optionTicker: 'O:AAPL260220C00235000', contractType: 'call', strikePrice: 235, dailyBar: null, prevDayClose: null, changeFromPrevClose: null, changePercent: null, isAtm: false, relativeStrike: 5 },
      ];
      component.contractRows.set(rows);

      expect(component.callRows().length).toBe(2);
      expect(component.putRows().length).toBe(1);
    });

    it('should sort calls by strike price ascending', () => {
      const rows: ContractRow[] = [
        { optionTicker: 'C235', contractType: 'call', strikePrice: 235, dailyBar: null, prevDayClose: null, changeFromPrevClose: null, changePercent: null, isAtm: false, relativeStrike: 5 },
        { optionTicker: 'C225', contractType: 'call', strikePrice: 225, dailyBar: null, prevDayClose: null, changeFromPrevClose: null, changePercent: null, isAtm: false, relativeStrike: -5 },
        { optionTicker: 'C230', contractType: 'call', strikePrice: 230, dailyBar: null, prevDayClose: null, changeFromPrevClose: null, changePercent: null, isAtm: true, relativeStrike: 0 },
      ];
      component.contractRows.set(rows);

      const sorted = component.callRows();
      expect(sorted[0].strikePrice).toBe(225);
      expect(sorted[1].strikePrice).toBe(230);
      expect(sorted[2].strikePrice).toBe(235);
    });
  });

  describe('computed: strikes', () => {
    it('should return unique sorted strikes', () => {
      const rows: ContractRow[] = [
        { optionTicker: 'C230', contractType: 'call', strikePrice: 230, dailyBar: null, prevDayClose: null, changeFromPrevClose: null, changePercent: null, isAtm: true, relativeStrike: 0 },
        { optionTicker: 'P230', contractType: 'put', strikePrice: 230, dailyBar: null, prevDayClose: null, changeFromPrevClose: null, changePercent: null, isAtm: true, relativeStrike: 0 },
        { optionTicker: 'C235', contractType: 'call', strikePrice: 235, dailyBar: null, prevDayClose: null, changeFromPrevClose: null, changePercent: null, isAtm: false, relativeStrike: 5 },
      ];
      component.contractRows.set(rows);

      const strikes = component.strikes();
      expect(strikes).toEqual([230, 235]);
    });
  });

  describe('isAtm', () => {
    it('should return true for ATM strike', () => {
      component.atmStrikeValue.set(230);
      expect(component.isAtm(230)).toBe(true);
    });

    it('should return false for non-ATM strike', () => {
      component.atmStrikeValue.set(230);
      expect(component.isAtm(235)).toBe(false);
    });
  });

  describe('analyze validation', () => {
    it('should set error for empty ticker', async () => {
      component.ticker.set('');
      await component.analyze();
      expect(component.error()).toBe('Enter a ticker symbol.');
    });

    it('should set error for whitespace-only ticker', async () => {
      component.ticker.set('   ');
      await component.analyze();
      expect(component.error()).toBe('Enter a ticker symbol.');
    });
  });

  describe('loadMinuteDetail', () => {
    it('should toggle expanded contract off when same ticker clicked', async () => {
      component.expandedContract.set('O:AAPL260220C00230000');
      await component.loadMinuteDetail('O:AAPL260220C00230000');

      expect(component.expandedContract()).toBeNull();
      expect(component.detailBars()).toEqual([]);
    });

    it('should expand and load data for new ticker', () => {
      component.analysisDate.set('2026-02-17');
      const promise = component.loadMinuteDetail('O:AAPL260220C00230000');

      expect(component.expandedContract()).toBe('O:AAPL260220C00230000');
      expect(component.detailLoading()).toBe(true);

      // Respond to the GraphQL request
      const req = httpMock.expectOne('http://localhost:5000/graphql');
      req.flush({
        data: {
          getOrFetchStockAggregates: {
            ticker: 'O:AAPL260220C00230000',
            aggregates: [],
            summary: null,
          },
        },
      });

      return promise.then(() => {
        expect(component.detailLoading()).toBe(false);
      });
    });
  });

  describe('getMarketDataLink', () => {
    it('should return link params for an option ticker', () => {
      component.analysisDate.set('2026-02-17');
      const link = component.getMarketDataLink('O:AAPL260220C00230000');

      expect(link['ticker']).toBe('O:AAPL260220C00230000');
      expect(link['toDate']).toBe('2026-02-17');
      expect(link['timespan']).toBe('minute');
    });
  });

  describe('selectedScanCount', () => {
    it('should count selected scan results', () => {
      const scans: ScanResult[] = [
        { strikePrice: 230, callTicker: 'C', callHasData: true, putTicker: 'P', putHasData: true, selected: true },
        { strikePrice: 235, callTicker: 'C', callHasData: true, putTicker: 'P', putHasData: false, selected: false },
        { strikePrice: 225, callTicker: 'C', callHasData: false, putTicker: 'P', putHasData: true, selected: true },
      ];
      component.scanResults.set(scans);

      expect(component.selectedScanCount()).toBe(2);
    });
  });
});
