import { ComponentFixture, TestBed } from '@angular/core/testing';
import { type Mock, vi } from 'vitest';
import { VolumeChartComponent } from './volume-chart.component';
import { createMockAggregate } from '../../../../testing/factories/market-data.factory';
import { createChart } from 'lightweight-charts';

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

describe('VolumeChartComponent', () => {
  let component: VolumeChartComponent;
  let fixture: ComponentFixture<VolumeChartComponent>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    (createChart as Mock).mockClear();

    await TestBed.configureTestingModule({
      imports: [VolumeChartComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(VolumeChartComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should call createChart on AfterViewInit', () => {
    fixture.componentRef.setInput('data', [createMockAggregate()]);
    fixture.detectChanges();
    expect(createChart).toHaveBeenCalledTimes(1);
  });

  it('should color green when close >= open', () => {
    fixture.componentRef.setInput('data', [
      createMockAggregate({ open: 150, close: 155, timestamp: '2026-01-01T00:00:00Z' }),
    ]);
    fixture.detectChanges();

    const chartInstance = (createChart as Mock).mock.results[0].value;
    const series = chartInstance.addSeries.mock.results[0].value;
    const passedData = series.setData.mock.calls[0][0];

    expect(passedData[0].color).toBe('#26a69a');
  });

  it('should color red when close < open', () => {
    fixture.componentRef.setInput('data', [
      createMockAggregate({ open: 155, close: 150, timestamp: '2026-01-01T00:00:00Z' }),
    ]);
    fixture.detectChanges();

    const chartInstance = (createChart as Mock).mock.results[0].value;
    const series = chartInstance.addSeries.mock.results[0].value;
    const passedData = series.setData.mock.calls[0][0];

    expect(passedData[0].color).toBe('#ef5350');
  });

  it('should clean up chart on destroy', () => {
    fixture.componentRef.setInput('data', [createMockAggregate()]);
    fixture.detectChanges();

    const chartInstance = (createChart as Mock).mock.results[0].value;
    fixture.destroy();
    expect(chartInstance.remove).toHaveBeenCalled();
  });
});
