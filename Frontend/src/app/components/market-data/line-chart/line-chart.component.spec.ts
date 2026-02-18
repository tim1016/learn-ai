import { ComponentFixture, TestBed } from '@angular/core/testing';
import { type Mock, vi } from 'vitest';
import { LineChartComponent } from './line-chart.component';
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

describe('LineChartComponent', () => {
  let component: LineChartComponent;
  let fixture: ComponentFixture<LineChartComponent>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    (createChart as Mock).mockClear();

    await TestBed.configureTestingModule({
      imports: [LineChartComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(LineChartComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should call createChart on AfterViewInit', () => {
    fixture.componentRef.setInput('data', createMockAggregates(3));
    fixture.detectChanges();
    expect(createChart).toHaveBeenCalledTimes(1);
  });

  it('should pass close prices as line data values', () => {
    const aggregates = createMockAggregates(3);
    fixture.componentRef.setInput('data', aggregates);
    fixture.detectChanges();

    const chartInstance = (createChart as Mock).mock.results[0].value;
    const series = chartInstance.addSeries.mock.results[0].value;
    const passedData = series.setData.mock.calls[0][0];

    expect(passedData.length).toBe(3);
    expect(passedData[0]).toHaveProperty('value');
    expect(passedData[0]).toHaveProperty('time');
  });

  it('should clean up chart on destroy', () => {
    fixture.componentRef.setInput('data', createMockAggregates(3));
    fixture.detectChanges();

    const chartInstance = (createChart as Mock).mock.results[0].value;
    fixture.destroy();
    expect(chartInstance.remove).toHaveBeenCalled();
  });
});
