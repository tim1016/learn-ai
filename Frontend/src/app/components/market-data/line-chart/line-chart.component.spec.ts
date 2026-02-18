import { ComponentFixture, TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { LineChartComponent } from './line-chart.component';

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

    await TestBed.configureTestingModule({
      imports: [LineChartComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(LineChartComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
