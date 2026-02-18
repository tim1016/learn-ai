import { ComponentFixture, TestBed } from '@angular/core/testing';
import { type Mock, vi } from 'vitest';
import { CandlestickChartComponent } from './candlestick-chart.component';
import { createMockAggregates } from '../../../../testing/factories/market-data.factory';
import { createChart } from 'lightweight-charts';

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

describe('CandlestickChartComponent', () => {
  let component: CandlestickChartComponent;
  let fixture: ComponentFixture<CandlestickChartComponent>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    (createChart as Mock).mockClear();

    await TestBed.configureTestingModule({
      imports: [CandlestickChartComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(CandlestickChartComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should call createChart on AfterViewInit', () => {
    component.data = createMockAggregates(5);
    fixture.detectChanges();
    expect(createChart).toHaveBeenCalledTimes(1);
  });

  it('should pass data to series.setData', () => {
    component.data = createMockAggregates(3);
    fixture.detectChanges();

    const chartInstance = (createChart as Mock).mock.results[0].value;
    const series = chartInstance.addSeries.mock.results[0].value;
    expect(series.setData).toHaveBeenCalled();

    const passedData = series.setData.mock.calls[0][0];
    expect(passedData.length).toBe(3);
    expect(passedData[0]).toHaveProperty('open');
    expect(passedData[0]).toHaveProperty('high');
    expect(passedData[0]).toHaveProperty('low');
    expect(passedData[0]).toHaveProperty('close');
    expect(passedData[0]).toHaveProperty('time');
  });

  it('should sort data by time ascending', () => {
    component.data = createMockAggregates(5);
    fixture.detectChanges();

    const chartInstance = (createChart as Mock).mock.results[0].value;
    const series = chartInstance.addSeries.mock.results[0].value;
    const passedData = series.setData.mock.calls[0][0];

    for (let i = 1; i < passedData.length; i++) {
      expect(passedData[i].time).toBeGreaterThan(passedData[i - 1].time);
    }
  });

  it('should clean up chart on destroy', () => {
    component.data = createMockAggregates(5);
    fixture.detectChanges();

    const chartInstance = (createChart as Mock).mock.results[0].value;
    fixture.destroy();
    expect(chartInstance.remove).toHaveBeenCalled();
  });
});
