import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { firstValueFrom } from 'rxjs';
import { ResearchService, ResearchResult, ResearchExperiment, SignalEngineResult, SignalExperiment } from './research.service';
import { environment } from '../../environments/environment';

describe('ResearchService', () => {
  let service: ResearchService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(ResearchService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

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
      icValues: [0.12, 0.18, 0.14],
      icDates: ['2024-01-01', '2024-01-02', '2024-01-03'],
      adfPvalue: 0.001,
      kpssPvalue: 0.3,
      isStationary: true,
      quantileBins: [
        { binNumber: 1, lowerBound: -0.02, upperBound: -0.005, meanReturn: -0.008, count: 40 },
        { binNumber: 2, lowerBound: -0.005, upperBound: 0.0, meanReturn: -0.002, count: 40 },
        { binNumber: 3, lowerBound: 0.0, upperBound: 0.005, meanReturn: 0.001, count: 40 },
        { binNumber: 4, lowerBound: 0.005, upperBound: 0.01, meanReturn: 0.004, count: 40 },
        { binNumber: 5, lowerBound: 0.01, upperBound: 0.02, meanReturn: 0.009, count: 40 },
      ],
      isMonotonic: true,
      monotonicityRatio: 1.0,
      nwTStat: 2.3,
      nwPValue: 0.025,
      effectiveN: 180,
      passedValidation: true,
    };
  }

  describe('runFeatureResearch', () => {
    it('should POST GraphQL mutation and return result', () => {
      const mockResult = createMockResult();

      service
        .runFeatureResearch({
          ticker: 'AAPL',
          featureName: 'momentum_5m',
          fromDate: '2024-01-01',
          toDate: '2024-03-31',
        })
        .subscribe(result => {
          expect(result.success).toBe(true);
          expect(result.ticker).toBe('AAPL');
          expect(result.meanIC).toBe(0.15);
          expect(result.quantileBins).toHaveLength(5);
        });

      const req = httpMock.expectOne(environment.backendUrl);
      expect(req.request.method).toBe('POST');
      expect(req.request.body.query).toContain('runFeatureResearch');
      expect(req.request.body.variables.ticker).toBe('AAPL');

      req.flush({ data: { runFeatureResearch: mockResult } });
    });

    it('should throw on GraphQL errors', () => {
      service
        .runFeatureResearch({
          ticker: 'AAPL',
          featureName: 'momentum_5m',
          fromDate: '2024-01-01',
          toDate: '2024-03-31',
        })
        .subscribe({
          error: err => {
            expect(err.message).toContain('Something went wrong');
          },
        });

      const req = httpMock.expectOne(environment.backendUrl);
      req.flush({
        data: { runFeatureResearch: null },
        errors: [{ message: 'Something went wrong' }],
      });
    });
  });

  describe('getExperiments', () => {
    it('should POST GraphQL query and return experiments list', () => {
      const mockExperiments: ResearchExperiment[] = [
        {
          id: 1,
          ticker: 'AAPL',
          featureName: 'momentum_5m',
          startDate: '2024-01-01',
          endDate: '2024-03-31',
          barsUsed: 200,
          meanIC: 0.15,
          icTStat: 2.5,
          icPValue: 0.02,
          adfPValue: 0.001,
          kpssPValue: 0.3,
          isStationary: true,
          passedValidation: true,
          monotonicityRatio: 1.0,
          isMonotonic: true,
          createdAt: '2024-04-01T00:00:00Z',
        },
      ];

      service.getExperiments('AAPL').subscribe(exps => {
        expect(exps).toHaveLength(1);
        expect(exps[0].featureName).toBe('momentum_5m');
      });

      const req = httpMock.expectOne(environment.backendUrl);
      expect(req.request.body.query).toContain('getResearchExperiments');
      req.flush({ data: { getResearchExperiments: mockExperiments } });
    });
  });

  describe('getExperiment', () => {
    it('should return null when experiment not found', () => {
      service.getExperiment(999).subscribe(exp => {
        expect(exp).toBeNull();
      });

      const req = httpMock.expectOne(environment.backendUrl);
      req.flush({ data: { getResearchExperiment: null } });
    });
  });

  describe('runSignalEngine', () => {
    it('should POST GraphQL mutation with correct variables', () => {
      service
        .runSignalEngine({
          ticker: 'AAPL',
          featureName: 'momentum_5m',
          fromDate: '2024-01-01',
          toDate: '2024-03-31',
          flipSign: true,
          regimeGateEnabled: true,
        })
        .subscribe();

      const req = httpMock.expectOne(environment.backendUrl);
      expect(req.request.method).toBe('POST');
      expect(req.request.body.query).toContain('runSignalEngine');
      expect(req.request.body.variables.ticker).toBe('AAPL');
      expect(req.request.body.variables.flipSign).toBe(true);
      req.flush({
        data: {
          runSignalEngine: {
            success: true, ticker: 'AAPL', featureName: 'momentum_5m',
            startDate: '2024-01-01', endDate: '2024-03-31', barsUsed: 200,
            flipSign: true, thresholdsTested: [0.5, 1.0], costBpsOptions: [5],
            bestThreshold: 1.0, bestCostBps: 5, backtestGrid: [],
            walkForward: null, graduation: null, signalDiagnostics: null,
            dataSufficiency: null, effectiveSample: null, regimeCoverage: [],
            signalBehavior: null, methodology: null, researchLog: '',
          },
        },
      });
    });

    it('should map response and return result', async () => {
      const promise = firstValueFrom(
        service.runSignalEngine({
          ticker: 'AAPL', featureName: 'momentum_5m',
          fromDate: '2024-01-01', toDate: '2024-03-31',
          flipSign: true, regimeGateEnabled: true,
        })
      );

      httpMock.expectOne(environment.backendUrl).flush({
        data: {
          runSignalEngine: {
            success: true, ticker: 'AAPL', featureName: 'momentum_5m',
            startDate: '2024-01-01', endDate: '2024-03-31', barsUsed: 200,
            flipSign: true, thresholdsTested: [0.5, 1.0], costBpsOptions: [5],
            bestThreshold: 1.0, bestCostBps: 5, backtestGrid: [],
            walkForward: null, graduation: { overallPassed: true, overallGrade: 'B', summary: 'OK', statusLabel: 'Candidate', parameterStability: null, criteria: [] },
            signalDiagnostics: null, dataSufficiency: null, effectiveSample: null,
            regimeCoverage: [], signalBehavior: null, methodology: null, researchLog: '',
          },
        },
      });

      const result = await promise;
      expect(result.success).toBe(true);
      expect(result.bestThreshold).toBe(1.0);
      expect(result.graduation?.overallGrade).toBe('B');
    });

    it('should throw on GraphQL errors', async () => {
      const promise = firstValueFrom(
        service.runSignalEngine({
          ticker: 'AAPL', featureName: 'bad_feature',
          fromDate: '2024-01-01', toDate: '2024-03-31',
          flipSign: true, regimeGateEnabled: true,
        })
      );

      httpMock.expectOne(environment.backendUrl).flush({
        data: null,
        errors: [{ message: 'Signal engine failed' }],
      });

      await expect(promise).rejects.toThrow('Signal engine failed');
    });
  });

  describe('getSignalExperiments', () => {
    it('should POST query with ticker filter and return experiments', async () => {
      const mockExperiments: SignalExperiment[] = [
        {
          id: 1, ticker: 'AAPL', featureName: 'momentum_5m',
          startDate: '2024-01-01', endDate: '2024-03-31', barsUsed: 200,
          overallGrade: 'B', statusLabel: 'Candidate', overallPassed: true,
          meanOosSharpe: 0.8, bestThreshold: 1.0, bestCostBps: 5,
          flipSign: true, regimeGateEnabled: true, createdAt: '2024-04-01T00:00:00Z',
        },
      ];

      const promise = firstValueFrom(service.getSignalExperiments('AAPL'));

      const req = httpMock.expectOne(environment.backendUrl);
      expect(req.request.body.query).toContain('getSignalExperiments');
      expect(req.request.body.variables.ticker).toBe('AAPL');
      req.flush({ data: { getSignalExperiments: mockExperiments } });

      const result = await promise;
      expect(result).toHaveLength(1);
      expect(result[0].overallGrade).toBe('B');
    });
  });

  describe('getSignalExperimentReport', () => {
    it('should return report by id', async () => {
      const promise = firstValueFrom(service.getSignalExperimentReport(42));

      const req = httpMock.expectOne(environment.backendUrl);
      expect(req.request.body.query).toContain('getSignalExperimentReport');
      expect(req.request.body.variables.id).toBe(42);
      req.flush({
        data: {
          getSignalExperimentReport: {
            success: true, ticker: 'AAPL', featureName: 'momentum_5m',
            startDate: '2024-01-01', endDate: '2024-03-31', barsUsed: 200,
            flipSign: true, thresholdsTested: [], costBpsOptions: [],
            bestThreshold: 1.0, bestCostBps: 5, backtestGrid: [],
            walkForward: null, graduation: null, signalDiagnostics: null,
            dataSufficiency: null, effectiveSample: null, regimeCoverage: [],
            signalBehavior: null, methodology: null, researchLog: '',
          },
        },
      });

      const result = await promise;
      expect(result).not.toBeNull();
      expect(result!.ticker).toBe('AAPL');
    });

    it('should return null when not found', async () => {
      const promise = firstValueFrom(service.getSignalExperimentReport(999));

      httpMock.expectOne(environment.backendUrl).flush({
        data: { getSignalExperimentReport: null },
      });

      const result = await promise;
      expect(result).toBeNull();
    });
  });
});
