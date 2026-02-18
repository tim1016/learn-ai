import { ComponentFixture, TestBed } from '@angular/core/testing';
import { vi } from 'vitest';
import { CandlestickChartComponent } from './candlestick-chart.component';

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

    await TestBed.configureTestingModule({
      imports: [CandlestickChartComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(CandlestickChartComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
