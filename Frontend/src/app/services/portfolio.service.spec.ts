import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { firstValueFrom } from 'rxjs';
import { PortfolioService } from './portfolio.service';
import { environment } from '../../environments/environment';

const GRAPHQL_URL = environment.backendUrl;

describe('PortfolioService', () => {
  let service: PortfolioService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(PortfolioService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  // ── getAccounts ──

  describe('getAccounts', () => {
    it('should send POST to GraphQL endpoint', () => {
      service.getAccounts().subscribe();

      const req = httpMock.expectOne(GRAPHQL_URL);
      expect(req.request.method).toBe('POST');
      req.flush({ data: { getAccounts: [] } });
    });

    it('should map response to Account array', async () => {
      const mockAccounts = [
        { id: 'acc-1', name: 'Paper Trading', type: 'Paper', baseCurrency: 'USD', initialCash: 100000, cash: 95000, createdAt: '2026-01-01' },
        { id: 'acc-2', name: 'Live Account', type: 'Live', baseCurrency: 'USD', initialCash: 50000, cash: 52000, createdAt: '2026-02-01' },
      ];

      const promise = firstValueFrom(service.getAccounts());
      httpMock.expectOne(GRAPHQL_URL).flush({ data: { getAccounts: mockAccounts } });

      const result = await promise;
      expect(result.length).toBe(2);
      expect(result[0].name).toBe('Paper Trading');
      expect(result[1].type).toBe('Live');
    });

    it('should return empty array when no accounts exist', async () => {
      const promise = firstValueFrom(service.getAccounts());
      httpMock.expectOne(GRAPHQL_URL).flush({ data: { getAccounts: [] } });

      const result = await promise;
      expect(result).toEqual([]);
    });
  });

  // ── createAccount ──

  describe('createAccount', () => {
    it('should send mutation with correct variables', () => {
      service.createAccount('Test Account', 'Paper', 50000).subscribe();

      const req = httpMock.expectOne(GRAPHQL_URL);
      expect(req.request.body.variables).toEqual({
        name: 'Test Account',
        type: 'Paper',
        initialCash: 50000,
      });
      expect(req.request.body.query).toContain('createAccount');
      req.flush({
        data: {
          createAccount: {
            success: true, error: null,
            account: { id: 'new-1', name: 'Test Account', type: 'Paper', cash: 50000, initialCash: 50000, createdAt: '2026-03-06' },
          },
        },
      });
    });

    it('should map successful response to AccountResult', async () => {
      const promise = firstValueFrom(service.createAccount('My Account', 'Live', 100000));

      httpMock.expectOne(GRAPHQL_URL).flush({
        data: {
          createAccount: {
            success: true, error: null,
            account: { id: 'acc-99', name: 'My Account', type: 'Live', cash: 100000, initialCash: 100000, createdAt: '2026-03-06' },
          },
        },
      });

      const result = await promise;
      expect(result.success).toBe(true);
      expect(result.account?.name).toBe('My Account');
      expect(result.account?.id).toBe('acc-99');
    });

    it('should map error response', async () => {
      const promise = firstValueFrom(service.createAccount('', 'Paper', 0));

      httpMock.expectOne(GRAPHQL_URL).flush({
        data: {
          createAccount: { success: false, error: 'Name is required', account: null },
        },
      });

      const result = await promise;
      expect(result.success).toBe(false);
      expect(result.error).toBe('Name is required');
    });
  });

  // ── getPortfolioState ──

  describe('getPortfolioState', () => {
    it('should send query with correct accountId', () => {
      service.getPortfolioState('acc-1').subscribe();

      const req = httpMock.expectOne(GRAPHQL_URL);
      expect(req.request.body.variables).toEqual({ accountId: 'acc-1' });
      expect(req.request.body.query).toContain('getPortfolioState');
      req.flush({
        data: {
          getPortfolioState: {
            account: { id: 'acc-1', name: 'Test', type: 'Paper', cash: 100000, initialCash: 100000, createdAt: '2026-01-01' },
            positions: [],
            recentTrades: [],
          },
        },
      });
    });

    it('should map response with positions and trades', async () => {
      const mockState = {
        account: { id: 'acc-1', name: 'Test', type: 'Paper', cash: 95000, initialCash: 100000, createdAt: '2026-01-01' },
        positions: [
          { id: 'pos-1', tickerId: 1, assetType: 'Stock', netQuantity: 100, avgCostBasis: 150, realizedPnL: 0, status: 'Open', openedAt: '2026-02-01', ticker: { symbol: 'AAPL', name: 'Apple' } },
        ],
        recentTrades: [
          { id: 'trd-1', tickerId: 1, side: 'Buy', quantity: 100, price: 150, fees: 1, multiplier: 1, executionTimestamp: '2026-02-01T10:00:00Z', ticker: { symbol: 'AAPL', name: 'Apple' } },
        ],
      };

      const promise = firstValueFrom(service.getPortfolioState('acc-1'));
      httpMock.expectOne(GRAPHQL_URL).flush({ data: { getPortfolioState: mockState } });

      const result = await promise;
      expect(result.account.cash).toBe(95000);
      expect(result.positions.length).toBe(1);
      expect(result.positions[0].ticker?.symbol).toBe('AAPL');
      expect(result.recentTrades.length).toBe(1);
      expect(result.recentTrades[0].side).toBe('Buy');
    });
  });

  // ── recordTrade ──

  describe('recordTrade', () => {
    it('should send mutation with all parameters', () => {
      service.recordTrade('acc-1', 42, 'Buy', 100, 155.50, 1.25, 'Stock', 1).subscribe();

      const req = httpMock.expectOne(GRAPHQL_URL);
      expect(req.request.body.variables).toEqual({
        accountId: 'acc-1',
        tickerId: 42,
        side: 'Buy',
        quantity: 100,
        price: 155.50,
        fees: 1.25,
        assetType: 'Stock',
        multiplier: 1,
      });
      expect(req.request.body.query).toContain('recordTrade');
      req.flush({
        data: {
          recordTrade: {
            success: true, error: null,
            trade: { id: 'trd-1', side: 'Buy', quantity: 100, price: 155.50, executionTimestamp: '2026-03-06T12:00:00Z', ticker: { symbol: 'AAPL' } },
          },
        },
      });
    });

    it('should use default values for fees, assetType, multiplier', () => {
      service.recordTrade('acc-1', 10, 'Sell', 50, 200).subscribe();

      const req = httpMock.expectOne(GRAPHQL_URL);
      expect(req.request.body.variables.fees).toBe(0);
      expect(req.request.body.variables.assetType).toBe('Stock');
      expect(req.request.body.variables.multiplier).toBe(1);
      req.flush({
        data: {
          recordTrade: {
            success: true, error: null,
            trade: { id: 'trd-2', side: 'Sell', quantity: 50, price: 200, executionTimestamp: '2026-03-06T12:00:00Z', ticker: { symbol: 'MSFT' } },
          },
        },
      });
    });

    it('should map successful trade result', async () => {
      const promise = firstValueFrom(service.recordTrade('acc-1', 1, 'Buy', 100, 150));

      httpMock.expectOne(GRAPHQL_URL).flush({
        data: {
          recordTrade: {
            success: true, error: null,
            trade: { id: 'trd-1', side: 'Buy', quantity: 100, price: 150, executionTimestamp: '2026-03-06T12:00:00Z', ticker: { symbol: 'AAPL' } },
          },
        },
      });

      const result = await promise;
      expect(result.success).toBe(true);
      expect(result.trade?.side).toBe('Buy');
      expect(result.trade?.ticker?.symbol).toBe('AAPL');
    });
  });

  // ── takeSnapshot ──

  describe('takeSnapshot', () => {
    it('should send mutation with accountId', () => {
      service.takeSnapshot('acc-1').subscribe();

      const req = httpMock.expectOne(GRAPHQL_URL);
      expect(req.request.body.variables).toEqual({ accountId: 'acc-1' });
      expect(req.request.body.query).toContain('takePortfolioSnapshot');
      req.flush({
        data: {
          takePortfolioSnapshot: {
            success: true, error: null, message: 'Snapshot taken',
            snapshot: { id: 'snap-1', timestamp: '2026-03-06T12:00:00Z', equity: 100000, cash: 95000, marketValue: 5000, unrealizedPnL: 200, realizedPnL: 50 },
          },
        },
      });
    });

    it('should map snapshot result', async () => {
      const promise = firstValueFrom(service.takeSnapshot('acc-1'));

      httpMock.expectOne(GRAPHQL_URL).flush({
        data: {
          takePortfolioSnapshot: {
            success: true, error: null, message: 'Snapshot saved',
            snapshot: { id: 'snap-1', timestamp: '2026-03-06T12:00:00Z', equity: 102000, cash: 90000, marketValue: 12000, unrealizedPnL: 500, realizedPnL: 100 },
          },
        },
      });

      const result = await promise;
      expect(result.success).toBe(true);
      expect(result.message).toBe('Snapshot saved');
      expect(result.snapshot?.equity).toBe(102000);
    });
  });

  // ── getRiskRules ──

  describe('getRiskRules', () => {
    it('should send query with accountId', () => {
      service.getRiskRules('acc-1').subscribe();

      const req = httpMock.expectOne(GRAPHQL_URL);
      expect(req.request.body.variables).toEqual({ accountId: 'acc-1' });
      expect(req.request.body.query).toContain('getRiskRules');
      req.flush({
        data: { getRiskRules: [] },
      });
    });

    it('should map risk rules response', async () => {
      const mockRules = [
        { id: 'rule-1', ruleType: 'MaxDrawdown', threshold: 0.10, action: 'Warn', severity: 'High', enabled: true, lastTriggered: null },
        { id: 'rule-2', ruleType: 'MaxPosition', threshold: 50000, action: 'Block', severity: 'Medium', enabled: true, lastTriggered: '2026-03-01' },
      ];

      const promise = firstValueFrom(service.getRiskRules('acc-1'));
      httpMock.expectOne(GRAPHQL_URL).flush({ data: { getRiskRules: mockRules } });

      const result = await promise;
      expect(result.length).toBe(2);
      expect(result[0].ruleType).toBe('MaxDrawdown');
      expect(result[1].action).toBe('Block');
    });
  });

  // ── Error handling ──

  describe('error handling', () => {
    it('should throw on GraphQL errors in response', async () => {
      const promise = firstValueFrom(service.getAccounts());

      httpMock.expectOne(GRAPHQL_URL).flush({
        data: null,
        errors: [{ message: 'Unauthorized' }],
      });

      await expect(promise).rejects.toThrow('Unauthorized');
    });

    it('should throw on GraphQL errors for mutations', async () => {
      const promise = firstValueFrom(service.createAccount('Test', 'Paper', 100000));

      httpMock.expectOne(GRAPHQL_URL).flush({
        data: null,
        errors: [{ message: 'Validation failed' }],
      });

      await expect(promise).rejects.toThrow('Validation failed');
    });

    it('should propagate HTTP errors', async () => {
      const promise = firstValueFrom(service.getAccounts());

      httpMock.expectOne(GRAPHQL_URL).error(
        new ProgressEvent('error'), { status: 500, statusText: 'Internal Server Error' },
      );

      await expect(promise).rejects.toThrow();
    });

    it('should throw on GraphQL errors for getPortfolioState', async () => {
      const promise = firstValueFrom(service.getPortfolioState('bad-id'));

      httpMock.expectOne(GRAPHQL_URL).flush({
        data: null,
        errors: [{ message: 'Account not found' }],
      });

      await expect(promise).rejects.toThrow('Account not found');
    });
  });

  // ── getPositions ──

  describe('getPositions', () => {
    it('should send query with accountId', () => {
      service.getPositions('acc-1').subscribe();

      const req = httpMock.expectOne(GRAPHQL_URL);
      expect(req.request.body.variables).toEqual({ accountId: 'acc-1' });
      expect(req.request.body.query).toContain('getPositions');
      req.flush({ data: { getPositions: [] } });
    });

    it('should map positions with lots', async () => {
      const mockPositions = [
        {
          id: 'pos-1', tickerId: 1, assetType: 'Stock', netQuantity: 100, avgCostBasis: 150,
          realizedPnL: 0, status: 'Open', openedAt: '2026-02-01', closedAt: null,
          ticker: { symbol: 'AAPL', name: 'Apple' },
          lots: [{ id: 'lot-1', quantity: 100, entryPrice: 150, remainingQuantity: 100, realizedPnL: 0, openedAt: '2026-02-01', closedAt: null }],
        },
      ];

      const promise = firstValueFrom(service.getPositions('acc-1'));
      httpMock.expectOne(GRAPHQL_URL).flush({ data: { getPositions: mockPositions } });

      const result = await promise;
      expect(result.length).toBe(1);
      expect(result[0].lots?.length).toBe(1);
      expect(result[0].lots?.[0].entryPrice).toBe(150);
    });
  });

  // ── getValuation ──

  describe('getValuation', () => {
    it('should send query and map response', async () => {
      const mockValuation = {
        cash: 90000, marketValue: 15000, equity: 105000,
        unrealizedPnL: 500, realizedPnL: 200,
        netDelta: 100, netGamma: 0.5, netTheta: -10, netVega: 25,
        positions: [{ symbol: 'AAPL', currentPrice: 155, quantity: 100, multiplier: 1, marketValue: 15500, unrealizedPnL: 500, costBasis: 15000 }],
      };

      const promise = firstValueFrom(service.getValuation('acc-1'));
      httpMock.expectOne(GRAPHQL_URL).flush({ data: { getPortfolioValuation: mockValuation } });

      const result = await promise;
      expect(result.equity).toBe(105000);
      expect(result.positions.length).toBe(1);
      expect(result.positions[0].symbol).toBe('AAPL');
    });
  });
});
