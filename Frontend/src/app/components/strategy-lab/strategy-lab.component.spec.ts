import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { StrategyLabComponent } from './strategy-lab.component';
import { BacktestResult } from '../../graphql/types';

describe('StrategyLabComponent', () => {
  let component: StrategyLabComponent;
  let fixture: ComponentFixture<StrategyLabComponent>;
  let httpMock: HttpTestingController;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [StrategyLabComponent],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();

    fixture = TestBed.createComponent(StrategyLabComponent);
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

  it('should have default signal values', () => {
    expect(component.ticker()).toBe('AAPL');
    expect(component.strategyName()).toBe('sma_crossover');
    expect(component.shortWindow()).toBe(10);
    expect(component.longWindow()).toBe(30);
    expect(component.rsiWindow()).toBe(14);
    expect(component.oversold()).toBe(30);
    expect(component.overbought()).toBe(70);
    expect(component.loading()).toBe(false);
    expect(component.error()).toBeNull();
    expect(component.result()).toBeNull();
  });

  it('should compute SMA parameters JSON', () => {
    component.strategyName.set('sma_crossover');
    component.shortWindow.set(5);
    component.longWindow.set(20);
    const json = JSON.parse(component.parametersJson());
    expect(json.ShortWindow).toBe(5);
    expect(json.LongWindow).toBe(20);
  });

  it('should compute RSI parameters JSON', () => {
    component.strategyName.set('rsi_mean_reversion');
    component.rsiWindow.set(10);
    component.oversold.set(25);
    component.overbought.set(75);
    const json = JSON.parse(component.parametersJson());
    expect(json.Window).toBe(10);
    expect(json.Oversold).toBe(25);
    expect(json.Overbought).toBe(75);
  });

  it('should compute win rate from result', () => {
    component.result.set({
      success: true, id: 1, strategyName: 'sma_crossover',
      parameters: '{}', totalTrades: 10, winningTrades: 7,
      losingTrades: 3, totalPnL: 50, maxDrawdown: 10,
      sharpeRatio: 1.5, durationMs: 100, trades: [], error: null,
    });
    expect(component.winRate()).toBe(70);
  });

  it('should compute avg PnL from result', () => {
    component.result.set({
      success: true, id: 1, strategyName: 'sma_crossover',
      parameters: '{}', totalTrades: 4, winningTrades: 3,
      losingTrades: 1, totalPnL: 20, maxDrawdown: 5,
      sharpeRatio: 1.0, durationMs: 50, trades: [], error: null,
    });
    expect(component.avgPnl()).toBe(5);
  });

  it('should return 0 win rate when no trades', () => {
    expect(component.winRate()).toBe(0);
  });

  it('should compute equity curve from trades', () => {
    component.result.set({
      success: true, id: 1, strategyName: 'sma_crossover',
      parameters: '{}', totalTrades: 2, winningTrades: 1,
      losingTrades: 1, totalPnL: 5, maxDrawdown: 3,
      sharpeRatio: 0.5, durationMs: 80, error: null,
      trades: [
        { tradeType: 'Buy', entryTimestamp: '2025-01-02T10:00:00', exitTimestamp: '2025-01-02T11:00:00', entryPrice: 100, exitPrice: 108, pnl: 8, cumulativePnl: 8, signalReason: 'Golden cross' },
        { tradeType: 'Buy', entryTimestamp: '2025-01-02T12:00:00', exitTimestamp: '2025-01-02T13:00:00', entryPrice: 108, exitPrice: 105, pnl: -3, cumulativePnl: 5, signalReason: 'Death cross' },
      ],
    });
    const curve = component.equityCurve();
    expect(curve.length).toBe(2);
    expect(curve[0].close).toBe(8);
    expect(curve[1].close).toBe(5);
  });

  it('should send backtest request and handle success', () => {
    const mockResult: BacktestResult = {
      success: true, id: 1, strategyName: 'sma_crossover',
      parameters: '{"ShortWindow":10,"LongWindow":30}',
      totalTrades: 3, winningTrades: 2, losingTrades: 1,
      totalPnL: 15.5, maxDrawdown: 4.2, sharpeRatio: 1.2,
      durationMs: 120, error: null,
      trades: [
        { tradeType: 'Buy', entryTimestamp: '2025-01-02T10:00:00', exitTimestamp: '2025-01-02T14:00:00', entryPrice: 100, exitPrice: 110, pnl: 10, cumulativePnl: 10, signalReason: 'Golden cross' },
      ],
    };

    component.runBacktest();
    expect(component.loading()).toBe(true);

    const req = httpMock.expectOne('http://localhost:5000/graphql');
    expect(req.request.method).toBe('POST');
    expect(req.request.body.variables.ticker).toBe('AAPL');
    expect(req.request.body.variables.strategyName).toBe('sma_crossover');

    req.flush({ data: { runBacktest: mockResult } });

    expect(component.loading()).toBe(false);
    expect(component.result()?.totalPnL).toBe(15.5);
    expect(component.error()).toBeNull();
  });

  it('should handle backtest error response', () => {
    component.runBacktest();

    const req = httpMock.expectOne('http://localhost:5000/graphql');
    req.flush({
      data: {
        runBacktest: {
          success: false, error: 'No aggregates found',
          id: null, strategyName: null, parameters: null,
          totalTrades: 0, winningTrades: 0, losingTrades: 0,
          totalPnL: 0, maxDrawdown: 0, sharpeRatio: 0,
          durationMs: 0, trades: [],
        },
      },
    });

    expect(component.loading()).toBe(false);
    expect(component.error()).toBe('No aggregates found');
    expect(component.result()).toBeNull();
  });

  it('should handle HTTP error', () => {
    component.runBacktest();

    const req = httpMock.expectOne('http://localhost:5000/graphql');
    req.error(new ProgressEvent('error'), { status: 500, statusText: 'Server Error' });

    expect(component.loading()).toBe(false);
    expect(component.error()).toBeTruthy();
  });

  it('should format prices correctly', () => {
    expect(component.formatPrice(123.456)).toBe('123.46');
    expect(component.formatPrice(-5.1)).toBe('-5.10');
    expect(component.formatPrice(0)).toBe('0.00');
  });

  it('should render strategy dropdown options', () => {
    const selects = fixture.nativeElement.querySelectorAll('select');
    // Find the select that contains strategy options
    let strategySelect: HTMLSelectElement | null = null;
    for (const sel of Array.from(selects) as HTMLSelectElement[]) {
      const opts = Array.from(sel.querySelectorAll('option')).map((o: any) => o.value);
      if (opts.includes('sma_crossover')) {
        strategySelect = sel;
        break;
      }
    }
    expect(strategySelect).toBeTruthy();
    const values = Array.from(strategySelect!.querySelectorAll('option')).map((o: any) => o.value);
    expect(values).toContain('sma_crossover');
    expect(values).toContain('rsi_mean_reversion');
  });

  it('should show SMA params by default', () => {
    fixture.detectChanges();
    const labels = fixture.nativeElement.textContent;
    expect(labels).toContain('Short Window');
    expect(labels).toContain('Long Window');
  });
});
