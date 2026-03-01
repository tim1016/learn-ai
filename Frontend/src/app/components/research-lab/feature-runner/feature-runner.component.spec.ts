import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpClientTestingModule } from '@angular/common/http/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { FeatureRunnerComponent } from './feature-runner.component';
import { ResearchService, ResearchResult } from '../../../services/research.service';
import { of, throwError } from 'rxjs';
import { vi } from 'vitest';

describe('FeatureRunnerComponent', () => {
  let component: FeatureRunnerComponent;
  let fixture: ComponentFixture<FeatureRunnerComponent>;
  let researchServiceMock: { runFeatureResearch: ReturnType<typeof vi.fn> };

  function createMockResult(): ResearchResult {
    return {
      success: true,
      ticker: 'AAPL',
      featureName: 'momentum_5m',
      startDate: '2024-01-01',
      endDate: '2024-03-31',
      barsUsed: 200,
      meanIC: 0.15,
      icTStat: 2.5,
      icPValue: 0.02,
      icValues: [0.12, 0.18],
      icDates: ['2024-01-01', '2024-01-02'],
      adfPvalue: 0.001,
      kpssPvalue: 0.3,
      isStationary: true,
      quantileBins: [],
      isMonotonic: true,
      monotonicityRatio: 1.0,
      nwTStat: 2.3,
      nwPValue: 0.025,
      effectiveN: 180,
      passedValidation: true,
    };
  }

  beforeEach(async () => {
    researchServiceMock = {
      runFeatureResearch: vi.fn(),
    };

    await TestBed.configureTestingModule({
      imports: [
        FeatureRunnerComponent,
        HttpClientTestingModule,
        NoopAnimationsModule,
      ],
      providers: [
        { provide: ResearchService, useValue: researchServiceMock },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(FeatureRunnerComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should have canRun true when all fields are filled and not loading', () => {
    component.ticker.set('AAPL');
    component.featureName.set('momentum_5m');
    component.fromDate.set('2024-01-01');
    component.toDate.set('2024-03-31');
    component.loading.set(false);

    expect(component.canRun()).toBe(true);
  });

  it('should have canRun false when ticker is empty', () => {
    component.ticker.set('');
    expect(component.canRun()).toBe(false);
  });

  it('should have canRun false when loading', () => {
    component.loading.set(true);
    expect(component.canRun()).toBe(false);
  });

  it('should call researchService and set result on success', () => {
    const mockResult = createMockResult();
    researchServiceMock.runFeatureResearch.mockReturnValue(of(mockResult));

    component.runResearch();

    expect(researchServiceMock.runFeatureResearch).toHaveBeenCalledOnce();
    expect(component.result()).toEqual(mockResult);
    expect(component.loading()).toBe(false);
    expect(component.error()).toBeNull();
  });

  it('should set error on service failure', () => {
    researchServiceMock.runFeatureResearch.mockReturnValue(
      throwError(() => new Error('Network error'))
    );

    component.runResearch();

    expect(component.error()).toBe('Network error');
    expect(component.result()).toBeNull();
    expect(component.loading()).toBe(false);
  });
});
