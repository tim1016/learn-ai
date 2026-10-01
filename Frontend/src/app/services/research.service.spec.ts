import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { firstValueFrom } from 'rxjs';
import { ResearchService, ResearchExperiment, SignalExperiment } from './research.service';
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
          createdAt: Date.UTC(2024, 3, 1),
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

  describe('getSignalExperiments', () => {
    it('should POST query with ticker filter and return experiments', async () => {
      const mockExperiments: SignalExperiment[] = [
        {
          id: 1, ticker: 'AAPL', featureName: 'momentum_5m',
          startDate: '2024-01-01', endDate: '2024-03-31', barsUsed: 200,
          overallGrade: 'B', statusLabel: 'Candidate', overallPassed: true,
          meanOosSharpe: 0.8, bestThreshold: 1.0, bestCostBps: 5,
          flipSign: true, regimeGateEnabled: true, createdAt: Date.UTC(2024, 3, 1),
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
      if (!result) throw new Error('Expected a signal experiment report');
      expect(result.ticker).toBe('AAPL');
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
