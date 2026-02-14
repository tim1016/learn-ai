import { ComponentFixture, TestBed } from '@angular/core/testing';
import { VolumeChartComponent } from './volume-chart.component';
import { createMockAggregate } from '../../../../testing/factories/market-data.factory';
import { createChart } from 'lightweight-charts';

describe('VolumeChartComponent', () => {
  let component: VolumeChartComponent;
  let fixture: ComponentFixture<VolumeChartComponent>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    (createChart as jest.Mock).mockClear();

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
    component.data = [createMockAggregate()];
    fixture.detectChanges();
    expect(createChart).toHaveBeenCalledTimes(1);
  });

  it('should color green when close >= open', () => {
    component.data = [
      createMockAggregate({ open: 150, close: 155, timestamp: '2026-01-01T00:00:00Z' }),
    ];
    fixture.detectChanges();

    const chartInstance = (createChart as jest.Mock).mock.results[0].value;
    const series = chartInstance.addSeries.mock.results[0].value;
    const passedData = series.setData.mock.calls[0][0];

    expect(passedData[0].color).toBe('#26a69a');
  });

  it('should color red when close < open', () => {
    component.data = [
      createMockAggregate({ open: 155, close: 150, timestamp: '2026-01-01T00:00:00Z' }),
    ];
    fixture.detectChanges();

    const chartInstance = (createChart as jest.Mock).mock.results[0].value;
    const series = chartInstance.addSeries.mock.results[0].value;
    const passedData = series.setData.mock.calls[0][0];

    expect(passedData[0].color).toBe('#ef5350');
  });

  it('should clean up chart on destroy', () => {
    component.data = [createMockAggregate()];
    fixture.detectChanges();

    const chartInstance = (createChart as jest.Mock).mock.results[0].value;
    fixture.destroy();
    expect(chartInstance.remove).toHaveBeenCalled();
  });
});
