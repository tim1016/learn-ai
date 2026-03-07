import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Component } from '@angular/core';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { DashboardComponent } from './dashboard.component';
import { environment } from '../../../../environments/environment';
import { PortfolioState, PortfolioMetrics } from '../../../graphql/portfolio-types';

const GRAPHQL_URL = environment.backendUrl;

const mockState: PortfolioState = {
  account: {
    id: 'acc-1', name: 'Paper Trading', type: 'Paper',
    baseCurrency: 'USD', initialCash: 100000, cash: 95000, createdAt: '2026-01-01',
  },
  positions: [
    {
      id: 'pos-1', accountId: 'acc-1', tickerId: 1, assetType: 'Stock',
      netQuantity: 100, avgCostBasis: 150, realizedPnL: 0, status: 'Open',
      openedAt: '2026-02-01', ticker: { symbol: 'AAPL', name: 'Apple' },
    },
    {
      id: 'pos-2', accountId: 'acc-1', tickerId: 2, assetType: 'Stock',
      netQuantity: 0, avgCostBasis: 200, realizedPnL: 500, status: 'Closed',
      openedAt: '2026-01-15', closedAt: '2026-02-15', ticker: { symbol: 'MSFT', name: 'Microsoft' },
    },
  ],
  recentTrades: [
    {
      id: 'trd-1', accountId: 'acc-1', tickerId: 1, side: 'Buy', quantity: 100,
      price: 150, fees: 1, multiplier: 1, executionTimestamp: '2026-02-01T10:00:00Z',
      ticker: { symbol: 'AAPL', name: 'Apple' },
    },
    {
      id: 'trd-2', accountId: 'acc-1', tickerId: 2, side: 'Sell', quantity: 50,
      price: 210, fees: 0.5, multiplier: 1, executionTimestamp: '2026-02-15T14:30:00Z',
      ticker: { symbol: 'MSFT', name: 'Microsoft' },
    },
  ],
};

const mockMetrics: PortfolioMetrics = {
  totalReturnPercent: 5.25,
  sharpeRatio: 1.234,
  sortinoRatio: 1.567,
  calmarRatio: 2.1,
  maxDrawdown: -5000,
  maxDrawdownPercent: -5.0,
  winRate: 0.6,
  profitFactor: 1.85,
  snapshotCount: 30,
};

// Host component to provide the required input
@Component({
  standalone: true,
  imports: [DashboardComponent],
  template: '<app-dashboard [accountId]="accountId" />',
})
class TestHostComponent {
  accountId = 'acc-1';
}

describe('DashboardComponent', () => {
  let hostFixture: ComponentFixture<TestHostComponent>;
  let httpMock: HttpTestingController;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [TestHostComponent],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();

    hostFixture = TestBed.createComponent(TestHostComponent);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  function getDashboardEl(): HTMLElement {
    return hostFixture.nativeElement as HTMLElement;
  }

  function getDashboard(): DashboardComponent {
    return hostFixture.debugElement.children[0].componentInstance;
  }

  /** Flush the two forkJoin requests triggered by the effect on accountId */
  function flushDashboardLoad(
    state: PortfolioState = mockState,
    metrics: PortfolioMetrics | null = mockMetrics,
  ): void {
    const reqs = httpMock.match(GRAPHQL_URL);
    for (const req of reqs) {
      const query: string = req.request.body.query;
      if (query.includes('getPortfolioState')) {
        req.flush({ data: { getPortfolioState: state } });
      } else if (query.includes('getPortfolioMetrics')) {
        if (metrics) {
          req.flush({ data: { getPortfolioMetrics: metrics } });
        } else {
          req.flush({ data: null, errors: [{ message: 'No metrics' }] });
        }
      } else {
        req.flush({ data: {} });
      }
    }
  }

  it('should create', () => {
    hostFixture.detectChanges();
    flushDashboardLoad();
    expect(getDashboard()).toBeTruthy();
  });

  it('should load dashboard data when accountId is provided', () => {
    hostFixture.detectChanges();

    const reqs = httpMock.match(GRAPHQL_URL);
    const queryBodies = reqs.map(r => r.request.body.query as string);
    expect(queryBodies.some(q => q.includes('getPortfolioState'))).toBe(true);
    expect(queryBodies.some(q => q.includes('getPortfolioMetrics'))).toBe(true);

    // Flush all
    reqs.forEach(r => {
      const q: string = r.request.body.query;
      if (q.includes('getPortfolioState')) {
        r.flush({ data: { getPortfolioState: mockState } });
      } else if (q.includes('getPortfolioMetrics')) {
        r.flush({ data: { getPortfolioMetrics: mockMetrics } });
      }
    });
  });

  it('should display account summary cards with correct values', () => {
    hostFixture.detectChanges();
    flushDashboardLoad();
    hostFixture.detectChanges();

    const el = getDashboardEl();
    const cards = el.querySelectorAll('.summary-cards .card');
    expect(cards.length).toBeGreaterThanOrEqual(4);

    const cardTexts = Array.from(cards).map(c => c.textContent?.trim() ?? '');
    const cashCard = cardTexts.find(t => t.includes('Cash'));
    expect(cashCard).toContain('95,000.00');

    const initialCard = cardTexts.find(t => t.includes('Initial Capital'));
    expect(initialCard).toContain('100,000.00');

    const openPosCard = cardTexts.find(t => t.includes('Open Positions'));
    expect(openPosCard).toContain('1');

    const tradesCard = cardTexts.find(t => t.includes('Recent Trades'));
    expect(tradesCard).toContain('2');
  });

  it('should display performance metrics', () => {
    hostFixture.detectChanges();
    flushDashboardLoad();
    hostFixture.detectChanges();

    const text = getDashboardEl().textContent ?? '';
    expect(text).toContain('5.25');
    expect(text).toContain('1.234');
    expect(text).toContain('1.567');
    expect(text).toContain('1.85');
  });

  it('should call takeSnapshot when button clicked', () => {
    hostFixture.detectChanges();
    flushDashboardLoad();
    hostFixture.detectChanges();

    const el = getDashboardEl();
    const snapshotBtn = Array.from(el.querySelectorAll('button'))
      .find(b => b.textContent?.includes('Take Snapshot'));
    expect(snapshotBtn).toBeDefined();

    snapshotBtn!.click();

    const req = httpMock.expectOne(GRAPHQL_URL);
    expect(req.request.body.query).toContain('takePortfolioSnapshot');
    expect(req.request.body.variables).toEqual({ accountId: 'acc-1' });
    req.flush({
      data: {
        takePortfolioSnapshot: {
          success: true, error: null, message: 'Snapshot taken',
          snapshot: { id: 'snap-1', timestamp: '2026-03-06T12:00:00Z', equity: 100000, cash: 95000, marketValue: 5000, unrealizedPnL: 200, realizedPnL: 50 },
        },
      },
    });

    hostFixture.detectChanges();

    const msg = el.querySelector('.snapshot-msg');
    expect(msg?.textContent).toContain('Snapshot taken');
  });

  it('should display snapshot error message on failure', () => {
    hostFixture.detectChanges();
    flushDashboardLoad();
    hostFixture.detectChanges();

    const el = getDashboardEl();
    const snapshotBtn = Array.from(el.querySelectorAll('button'))
      .find(b => b.textContent?.includes('Take Snapshot'));
    snapshotBtn!.click();

    const req = httpMock.expectOne(GRAPHQL_URL);
    req.flush({
      data: {
        takePortfolioSnapshot: {
          success: false, error: 'No positions to snapshot', message: null, snapshot: null,
        },
      },
    });
    hostFixture.detectChanges();

    const msg = el.querySelector('.snapshot-msg');
    expect(msg?.textContent).toContain('No positions to snapshot');
  });

  it('should render recent trades table', () => {
    hostFixture.detectChanges();
    flushDashboardLoad();
    hostFixture.detectChanges();

    const el = getDashboardEl();
    const rows = el.querySelectorAll('table tbody tr');
    expect(rows.length).toBe(2);

    const firstRow = rows[0].textContent ?? '';
    expect(firstRow).toContain('AAPL');
    expect(firstRow).toContain('Buy');
    expect(firstRow).toContain('100');

    const secondRow = rows[1].textContent ?? '';
    expect(secondRow).toContain('MSFT');
    expect(secondRow).toContain('Sell');
  });

  it('should render trade table headers', () => {
    hostFixture.detectChanges();
    flushDashboardLoad();
    hostFixture.detectChanges();

    const el = getDashboardEl();
    const headers = el.querySelectorAll('table thead th');
    const headerTexts = Array.from(headers).map(h => h.textContent?.trim());
    expect(headerTexts).toContain('Symbol');
    expect(headerTexts).toContain('Side');
    expect(headerTexts).toContain('Qty');
    expect(headerTexts).toContain('Price');
    expect(headerTexts).toContain('Fees');
    expect(headerTexts).toContain('Time');
  });

  it('should call recordTrade with form values', () => {
    hostFixture.detectChanges();
    flushDashboardLoad();

    const dashboard = getDashboard();
    dashboard.tradeTickerId.set(1);
    dashboard.tradeSide.set('Buy');
    dashboard.tradeQty.set(50);
    dashboard.tradePrice.set(175);
    dashboard.tradeFees.set(2.5);

    dashboard.recordTrade();

    const req = httpMock.expectOne(GRAPHQL_URL);
    expect(req.request.body.query).toContain('recordTrade');
    expect(req.request.body.variables.accountId).toBe('acc-1');
    expect(req.request.body.variables.tickerId).toBe(1);
    expect(req.request.body.variables.side).toBe('Buy');
    expect(req.request.body.variables.quantity).toBe(50);
    expect(req.request.body.variables.price).toBe(175);
    expect(req.request.body.variables.fees).toBe(2.5);

    req.flush({
      data: {
        recordTrade: {
          success: true, error: null,
          trade: { id: 'trd-new', side: 'Buy', quantity: 50, price: 175, executionTimestamp: '2026-03-06T12:00:00Z', ticker: { symbol: 'AAPL' } },
        },
      },
    });

    // recordTrade success triggers loadDashboard, flush those too
    flushDashboardLoad();
  });

  it('should not call recordTrade when tickerId is zero', () => {
    hostFixture.detectChanges();
    flushDashboardLoad();

    const dashboard = getDashboard();
    dashboard.tradeTickerId.set(0);
    dashboard.tradePrice.set(100);
    dashboard.recordTrade();

    // No additional requests should be made
    httpMock.expectNone(GRAPHQL_URL);
  });

  it('should not call recordTrade when price is zero', () => {
    hostFixture.detectChanges();
    flushDashboardLoad();

    const dashboard = getDashboard();
    dashboard.tradeTickerId.set(1);
    dashboard.tradePrice.set(0);
    dashboard.recordTrade();

    httpMock.expectNone(GRAPHQL_URL);
  });

  it('should reload dashboard after successful trade', () => {
    hostFixture.detectChanges();
    flushDashboardLoad();

    const dashboard = getDashboard();
    dashboard.tradeTickerId.set(1);
    dashboard.tradePrice.set(175);
    dashboard.recordTrade();

    // Flush the recordTrade mutation
    const tradeReq = httpMock.expectOne(GRAPHQL_URL);
    expect(tradeReq.request.body.query).toContain('recordTrade');
    tradeReq.flush({
      data: {
        recordTrade: {
          success: true, error: null,
          trade: { id: 'trd-new', side: 'Buy', quantity: 100, price: 175, executionTimestamp: '2026-03-06T12:00:00Z', ticker: { symbol: 'AAPL' } },
        },
      },
    });

    // After success, loadDashboard is called again - verify the state request fires
    const reloadReqs = httpMock.match(GRAPHQL_URL);
    const hasStateReload = reloadReqs.some(r => r.request.body.query.includes('getPortfolioState'));
    expect(hasStateReload).toBe(true);
    reloadReqs.forEach(r => {
      const q: string = r.request.body.query;
      if (q.includes('getPortfolioState')) {
        r.flush({ data: { getPortfolioState: mockState } });
      } else if (q.includes('getPortfolioMetrics')) {
        r.flush({ data: { getPortfolioMetrics: mockMetrics } });
      }
    });
  });

  it('should set error when trade fails', () => {
    hostFixture.detectChanges();
    flushDashboardLoad();

    const dashboard = getDashboard();
    dashboard.tradeTickerId.set(1);
    dashboard.tradePrice.set(175);
    dashboard.recordTrade();

    const req = httpMock.expectOne(GRAPHQL_URL);
    req.flush({
      data: {
        recordTrade: { success: false, error: 'Insufficient cash', trade: null },
      },
    });

    expect(dashboard.error()).toBe('Insufficient cash');
  });

  it('should show error message in the DOM', () => {
    hostFixture.detectChanges();
    flushDashboardLoad();

    const dashboard = getDashboard();
    dashboard.error.set('Something went wrong');
    hostFixture.detectChanges();

    const errorEl = getDashboardEl().querySelector('.error-msg');
    expect(errorEl).not.toBeNull();
    expect(errorEl?.textContent).toContain('Something went wrong');
  });

  it('should show Refresh button', () => {
    hostFixture.detectChanges();
    flushDashboardLoad();
    hostFixture.detectChanges();

    const el = getDashboardEl();
    const refreshBtn = Array.from(el.querySelectorAll('button'))
      .find(b => b.textContent?.includes('Refresh'));
    expect(refreshBtn).toBeDefined();
    expect(refreshBtn?.textContent?.trim()).toBe('Refresh');
  });

  it('should handle missing metrics gracefully', () => {
    hostFixture.detectChanges();
    flushDashboardLoad(mockState, null);
    hostFixture.detectChanges();

    const dashboard = getDashboard();
    expect(dashboard.metrics()).toBeNull();

    const el = getDashboardEl();
    expect(el.textContent).toContain('Cash');
    expect(el.textContent).not.toContain('Total Return');
  });

  it('should compute openPositionCount correctly', () => {
    hostFixture.detectChanges();
    flushDashboardLoad();

    const dashboard = getDashboard();
    // mockState has 1 Open and 1 Closed position
    expect(dashboard.openPositionCount).toBe(1);
  });

  it('should not show trades table when no recent trades', () => {
    const stateNoTrades: PortfolioState = {
      ...mockState,
      recentTrades: [],
    };

    hostFixture.detectChanges();
    flushDashboardLoad(stateNoTrades);
    hostFixture.detectChanges();

    const el = getDashboardEl();
    expect(el.querySelector('table')).toBeNull();
  });
});
