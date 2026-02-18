import { ComponentFixture, TestBed } from '@angular/core/testing';
import { expect, it, vi } from 'vitest';
import { TaChartComponent } from './ta-chart.component';
import { createMockAggregates, createMockIndicatorSeries } from '../../../../testing/factories/market-data.factory';

vi.mock('lightweight-charts', () => {
  const mockTimeScale = { fitContent: vi.fn(), applyOptions: vi.fn() };
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

describe('TaChartComponent', () => {
  let component: TaChartComponent;
  let fixture: ComponentFixture<TaChartComponent>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [TaChartComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(TaChartComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should compute empty candlestick data with no aggregates', () => {
    fixture.detectChanges();
    expect(component.candlestickData()).toEqual([]);
  });

  it('should compute candlestick data from aggregates', () => {
    fixture.componentRef.setInput('aggregates', createMockAggregates(3));
    fixture.detectChanges();

    const data = component.candlestickData();
    expect(data.length).toBe(3);
    expect(data[0]).toHaveProperty('time');
    expect(data[0]).toHaveProperty('open');
    expect(data[0]).toHaveProperty('high');
    expect(data[0]).toHaveProperty('low');
    expect(data[0]).toHaveProperty('close');
  });

  it('should sort candlestick data by time ascending', () => {
    fixture.componentRef.setInput('aggregates', createMockAggregates(5));
    fixture.detectChanges();

    const data = component.candlestickData();
    for (let i = 1; i < data.length; i++) {
      expect((data[i].time as number)).toBeGreaterThan((data[i - 1].time as number));
    }
  });

  it('should filter overlay indicators (sma, ema, bbands)', () => {
    fixture.componentRef.setInput('indicators', [
      createMockIndicatorSeries({ name: 'sma', window: 20 }),
      createMockIndicatorSeries({ name: 'rsi', window: 14 }),
      createMockIndicatorSeries({ name: 'ema', window: 50 }),
      createMockIndicatorSeries({ name: 'bbands', window: 20 }),
    ]);
    fixture.detectChanges();

    const overlays = component.overlayIndicators();
    expect(overlays.length).toBe(3);
    expect(overlays.map(o => o.name)).toEqual(['sma', 'ema', 'bbands']);
  });

  it('should identify RSI indicator', () => {
    fixture.componentRef.setInput('indicators', [
      createMockIndicatorSeries({ name: 'sma', window: 20 }),
      createMockIndicatorSeries({ name: 'rsi', window: 14 }),
    ]);
    fixture.detectChanges();

    expect(component.hasRsi()).toBe(true);
    expect(component.rsiIndicator()?.name).toBe('rsi');
    expect(component.rsiIndicator()?.window).toBe(14);
  });

  it('should return null for RSI when not present', () => {
    fixture.componentRef.setInput('indicators', [
      createMockIndicatorSeries({ name: 'sma', window: 20 }),
    ]);
    fixture.detectChanges();

    expect(component.hasRsi()).toBe(false);
    expect(component.rsiIndicator()).toBeNull();
  });

  it('should have empty overlays with no indicators', () => {
    fixture.detectChanges();
    expect(component.overlayIndicators()).toEqual([]);
  });
});
