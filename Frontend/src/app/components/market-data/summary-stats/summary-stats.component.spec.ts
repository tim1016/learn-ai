import { ComponentFixture, TestBed } from '@angular/core/testing';
import { SummaryStatsComponent } from './summary-stats.component';
import { createMockSummary } from '../../../../testing/factories/market-data.factory';

describe('SummaryStatsComponent', () => {
  let component: SummaryStatsComponent;
  let fixture: ComponentFixture<SummaryStatsComponent>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [SummaryStatsComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(SummaryStatsComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should render nothing when summary is null', () => {
    component.summary = null;
    fixture.detectChanges();
    const grid = fixture.nativeElement.querySelector('.stats-grid');
    expect(grid).toBeNull();
  });

  it('should render stat cards when summary is provided', () => {
    component.summary = createMockSummary();
    fixture.detectChanges();
    const cards = fixture.nativeElement.querySelectorAll('.stat-card');
    expect(cards.length).toBeGreaterThan(0);
  });

  it('should apply positive class for positive price change', () => {
    component.summary = createMockSummary({ priceChange: 5.0 });
    fixture.detectChanges();
    const el = fixture.nativeElement.querySelector('.positive');
    expect(el).toBeTruthy();
  });

  it('should apply negative class for negative price change', () => {
    component.summary = createMockSummary({ priceChange: -5.0 });
    fixture.detectChanges();
    const el = fixture.nativeElement.querySelector('.negative');
    expect(el).toBeTruthy();
  });

  it('should display period high and low', () => {
    component.summary = createMockSummary({ periodHigh: 200, periodLow: 140 });
    fixture.detectChanges();
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('200');
    expect(text).toContain('140');
  });
});
