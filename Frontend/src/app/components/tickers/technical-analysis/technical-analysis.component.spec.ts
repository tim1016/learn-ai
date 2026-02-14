import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { TechnicalAnalysisComponent } from './technical-analysis.component';

describe('TechnicalAnalysisComponent', () => {
  let component: TechnicalAnalysisComponent;
  let fixture: ComponentFixture<TechnicalAnalysisComponent>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [TechnicalAnalysisComponent],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();

    fixture = TestBed.createComponent(TechnicalAnalysisComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should have correct default signal values', () => {
    expect(component.ticker()).toBe('AAPL');
    expect(component.timespan()).toBe('day');
    expect(component.multiplier()).toBe(1);
  });

  it('should have indicator toggles enabled by default', () => {
    expect(component.showSma()).toBe(true);
    expect(component.showEma()).toBe(true);
    expect(component.showRsi()).toBe(true);
  });

  it('should have correct default indicator windows', () => {
    expect(component.smaWindow()).toBe(20);
    expect(component.emaWindow()).toBe(50);
    expect(component.rsiWindow()).toBe(14);
  });

  it('should have empty aggregates and indicators by default', () => {
    expect(component.aggregates()).toEqual([]);
    expect(component.indicators()).toEqual([]);
  });

  it('should have no error by default', () => {
    expect(component.error()).toBeNull();
    expect(component.message()).toBeNull();
  });

  it('should not trigger request when ticker is empty', () => {
    component.ticker.set('');
    component.fetchAndCalculate();
    expect(component.aggregates()).toEqual([]);
  });

  it('should set fromDate and toDate as valid date strings', () => {
    const from = component.fromDate();
    const to = component.toDate();
    expect(from).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(to).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(new Date(from).getTime()).toBeLessThan(new Date(to).getTime());
  });
});
