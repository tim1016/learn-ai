import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { StrategyLabComponent } from './strategy-lab.component';
import { environment } from '../../../environments/environment';

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
    // Flush any pending requests (e.g. holiday fetch triggered on init)
    httpMock.match(() => true).forEach(req => req.flush([]));
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

  it('should send backtest request to Python service', () => {
    component.runBacktest();
    expect(component.loading()).toBe(true);

    const req = httpMock.expectOne(`${environment.pythonServiceUrl}/api/backtest/run`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body.ticker).toBe('AAPL');
    expect(req.request.body.strategy_name).toBe('sma_crossover');

    req.flush({
      success: true,
      ticker: 'AAPL',
      strategy_name: 'sma_crossover',
      parameters: { ShortWindow: 10, LongWindow: 30 },
      total_trades: 3, winning_trades: 2, losing_trades: 1,
      win_rate: 66.67, avg_win_pct: 2.5, avg_loss_pct: -1.0,
      win_loss_ratio: 2.5, profit_factor: 5.0,
      expectancy_per_trade: 1.33,
      total_pnl_pct: 4.0, total_pnl_pts: 15.5,
      max_drawdown_pct: 2.1, sharpe_ratio: 1.2,
      lean_statistics: null,
      source_bars: 1000, rth_bars: 800, resampled_bars: 160,
      bars_processed: 160, timeframe: '5m',
      chart_bars: [], chart_indicators: [], quality: null,
      trades: [], error: null,
    });

    expect(component.loading()).toBe(false);
    expect(component.result()?.total_pnl_pts).toBe(15.5);
    expect(component.error()).toBeNull();
  });

  it('should handle backtest error response', () => {
    component.runBacktest();

    const req = httpMock.expectOne(`${environment.pythonServiceUrl}/api/backtest/run`);
    req.flush({
      success: false, error: 'No aggregates found',
      ticker: 'AAPL', strategy_name: 'sma_crossover', parameters: {},
      total_trades: 0, winning_trades: 0, losing_trades: 0,
      win_rate: 0, avg_win_pct: 0, avg_loss_pct: 0,
      win_loss_ratio: 0, profit_factor: 0, expectancy_per_trade: 0,
      total_pnl_pct: 0, total_pnl_pts: 0, max_drawdown_pct: 0,
      sharpe_ratio: 0, lean_statistics: null,
      source_bars: 0, rth_bars: 0, resampled_bars: 0,
      bars_processed: 0, timeframe: '5m',
      chart_bars: [], chart_indicators: [], quality: null,
      trades: [],
    });

    expect(component.loading()).toBe(false);
    expect(component.error()).toBe('No aggregates found');
    expect(component.result()).toBeNull();
  });

  it('should handle HTTP error', () => {
    component.runBacktest();

    const req = httpMock.expectOne(`${environment.pythonServiceUrl}/api/backtest/run`);
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
