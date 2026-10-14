import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { OptionsLabChainComponent } from './options-lab-chain.component';
import { fakePickerWorld } from '../../../shared/symbol-picker/testing/fake-picker-world';
import { SnapshotContractResult } from '../../../graphql/types';

function makeContract(
  contractType: 'call' | 'put',
  strike: number,
  overrides: Partial<SnapshotContractResult> = {},
): SnapshotContractResult {
  return {
    ticker: `O:SPY${strike}${contractType[0].toUpperCase()}`,
    contractType,
    strikePrice: strike,
    expirationMs: 1_778_875_200_000,
    breakEvenPrice: null,
    impliedVolatility: 0.2,
    openInterest: 100,
    greeks: { delta: 0.5, gamma: 0.01, theta: -0.05, vega: 0.1 },
    day: { open: 1, high: 1, low: 1, close: 1, volume: 1000, vwap: 1 },
    lastTrade: null,
    lastQuote: null,
    ...overrides,
  };
}

function drainAll(httpMock: HttpTestingController): void {
  httpMock.match(() => true).forEach(req => {
    if (!req.cancelled) req.flush({ data: {} });
  });
}

describe('OptionsLabChainComponent', () => {
  let component: OptionsLabChainComponent;
  let fixture: ComponentFixture<OptionsLabChainComponent>;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    localStorage.clear();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [OptionsLabChainComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        ...fakePickerWorld().providers,
      ],
    });
    fixture = TestBed.createComponent(OptionsLabChainComponent);
    component = fixture.componentInstance;
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    drainAll(httpMock);
    httpMock.verify();
  });

  it('detects the ATM strike as the strike closest to spot', () => {
    component.allContracts.set([
      makeContract('call', 720),
      makeContract('call', 723),
      makeContract('call', 725),
      makeContract('put', 720),
      makeContract('put', 723),
      makeContract('put', 725),
    ]);
    component.underlying.set({
      ticker: 'SPY', price: 723.77, change: 5.76, changePercent: 0.8,
    });

    const rows = component.rows();
    const atm = rows.filter(r => r.isAtm);
    expect(atm.length).toBe(1);
    expect(atm[0].strike).toBe(723);
  });

  it('flags ITM-call (strike < spot) and ITM-put (strike > spot) sides correctly', () => {
    // Strikes flanking spot 725 — 725 is ATM so 720 and 730 are unambiguously ITM-call / ITM-put.
    component.allContracts.set([
      makeContract('call', 720),
      makeContract('call', 725),
      makeContract('call', 730),
      makeContract('put', 720),
      makeContract('put', 725),
      makeContract('put', 730),
    ]);
    component.underlying.set({
      ticker: 'SPY', price: 725, change: 0, changePercent: 0,
    });

    const rows = component.rows();
    const r720 = rows.find(r => r.strike === 720);
    const r725 = rows.find(r => r.strike === 725);
    const r730 = rows.find(r => r.strike === 730);
    expect(r725?.isAtm).toBe(true);
    expect(r720?.itmCall).toBe(true);
    expect(r720?.itmPut).toBe(false);
    expect(r730?.itmCall).toBe(false);
    expect(r730?.itmPut).toBe(true);
  });

  it('limits visible strikes to ±N around ATM when showAllStrikes is false', () => {
    const strikes = Array.from({ length: 41 }, (_, i) => 700 + i); // 700..740
    const contracts: SnapshotContractResult[] = [];
    for (const k of strikes) {
      contracts.push(makeContract('call', k));
      contracts.push(makeContract('put', k));
    }
    component.allContracts.set(contracts);
    component.underlying.set({
      ticker: 'SPY', price: 720, change: 0, changePercent: 0,
    });
    component.setStrikeRange(5);

    const rows = component.rows();
    expect(rows.length).toBe(11); // ±5 + ATM
    expect(rows[0].strike).toBe(715);
    expect(rows[rows.length - 1].strike).toBe(725);
  });

  it('shows all strikes when showAllStrikes is toggled on', () => {
    const strikes = [710, 715, 720, 725, 730];
    component.allContracts.set(
      strikes.flatMap(k => [makeContract('call', k), makeContract('put', k)]),
    );
    component.underlying.set({
      ticker: 'SPY', price: 720, change: 0, changePercent: 0,
    });
    component.setStrikeRange(1);
    expect(component.rows().length).toBe(3);

    component.toggleShowAll();
    expect(component.rows().length).toBe(strikes.length);
  });

  it('persists density preference to localStorage', () => {
    expect(component.density()).toBe('quick');
    component.setDensity('greeks');
    expect(component.density()).toBe('greeks');
    expect(localStorage.getItem('optionsLab.chain.density')).toBe('greeks');
    component.setDensity('flow');
    expect(component.density()).toBe('flow');
    expect(localStorage.getItem('optionsLab.chain.density')).toBe('flow');
  });

  it('formats missing bid/ask as em-dash and shows volume when present', () => {
    component.allContracts.set([
      makeContract('call', 720, {
        lastQuote: null,
        day: { open: 0, high: 0, low: 0, close: 0, volume: 5000, vwap: 0 },
      }),
      makeContract('put', 720, {
        lastQuote: null,
        day: { open: 0, high: 0, low: 0, close: 0, volume: 0, vwap: 0 },
      }),
    ]);
    component.underlying.set({
      ticker: 'SPY', price: 720, change: 0, changePercent: 0,
    });

    const row = component.rows()[0];
    expect(row.callBid).toBe('—');
    expect(row.callAsk).toBe('—');
    expect(row.callVolume).toBe('5.0K');
    expect(row.putVolume).toBe('—');
  });

  it('skips polling when no expiration is selected', () => {
    expect(component.selectedExpiration()).toBeNull();
    drainAll(httpMock);
    const before = httpMock.match(() => true).length;
    expect(before).toBe(0);
  });

  // Regression for Codex P2 #2: a transient initial-load failure latched
  // error() non-null, and the previous polling guard `if (error() !== null)
  // return` then suppressed every subsequent refresh permanently. The fix
  // (a) drops that guard and (b) clears error() on a successful fetch so
  // the banner doesn't linger after a recovery.
  it('clears stale error signal after a successful chain fetch', async () => {
    component.ticker.set('SPY');
    component.selectedExpiration.set(1_778_875_200_000);
    component.error.set('previous failure');

    const fetchPromise = component.fetchChainSnapshot(true);

    const req = httpMock.expectOne(r =>
      r.url === 'http://localhost:5000/graphql' &&
      typeof (r.body as { query?: string } | null)?.query === 'string' &&
      (r.body as { query: string }).query.includes('getOptionsChainSnapshot'),
    );
    req.flush({
      data: {
        getOptionsChainSnapshot: {
          success: true,
          underlying: { ticker: 'SPY', price: 723.77, change: 5.76, changePercent: 0.8 },
          contracts: [],
          count: 0,
          riskFreeRate: null,
          dividendYield: null,
          rateSource: null,
          dividendSource: null,
          error: null,
        },
      },
    });

    await fetchPromise;
    expect(component.error()).toBeNull();
  });

  // ── Failures and stale data are shown, not hidden (#3221) ──
  describe('failures and stale quotes', () => {
    const NEAR = 4_070_984_400_000;
    const FAR = 4_073_576_400_000;

    function pending(needle: string, ticker?: string) {
      return httpMock.match(r => {
        const b = r.body as { query?: string; variables?: { underlyingTicker?: string; ticker?: string } } | null;
        return r.url === 'http://localhost:5000/graphql'
          && b?.query?.includes(needle) === true
          && (ticker === undefined || (b.variables?.underlyingTicker ?? b.variables?.ticker) === ticker);
      });
    }

    function chainReply(strike: number, price: number) {
      return {
        data: {
          getOptionsChainSnapshot: {
            success: true,
            underlying: { ticker: 'SPY', price, change: 0, changePercent: 0 },
            contracts: [makeContract('call', strike), makeContract('put', strike)],
            count: 2, riskFreeRate: null, dividendYield: null, rateSource: null, dividendSource: null, error: null,
          },
        },
      };
    }

    function chainFailure(error: string) {
      return {
        data: {
          getOptionsChainSnapshot: {
            success: false, underlying: null, contracts: [], count: 0,
            riskFreeRate: null, dividendYield: null, rateSource: null, dividendSource: null, error,
          },
        },
      };
    }

    function expirationsReply(expirations: number[]) {
      return { data: { getOptionsExpirations: { success: true, expirations, count: expirations.length, error: null } } };
    }

    const noSnapshot = { data: { getStockSnapshot: { success: false, snapshot: null, error: null } } };

    async function settle(): Promise<void> {
      await new Promise(resolve => setTimeout(resolve, 0));
      fixture.detectChanges();
    }

    /** Render the page and let its first expirations + chain load finish. */
    async function openPage(): Promise<void> {
      fixture.detectChanges();
      pending('getOptionsExpirations')[0].flush(expirationsReply([NEAR, FAR]));
      pending('getStockSnapshot')[0].flush(noSnapshot);
      await settle();
      pending('getOptionsChainSnapshot')[0].flush(chainReply(720, 723.77));
      await settle();
    }

    function text(selector: string): string | null {
      return fixture.nativeElement.querySelector(selector)?.textContent?.replace(/\s+/g, ' ').trim() ?? null;
    }

    it('shows the reason when the expirations lookup fails, not just an empty chain', async () => {
      fixture.detectChanges();
      pending('getOptionsExpirations')[0].flush({
        data: { getOptionsExpirations: { success: false, expirations: [], count: 0, error: 'Polygon rate limited' } },
      });
      pending('getStockSnapshot')[0].flush(noSnapshot);
      await settle();

      expect(text('.banner.error')).toContain('Polygon rate limited');
    });

    it('marks the quotes stale, with the reason, when a background refresh fails', async () => {
      await openPage();
      expect(text('.exp-asof')).toContain('Quotes as of');
      expect(fixture.nativeElement.querySelector('.exp-asof.stale')).toBeNull();

      const refresh = component.fetchChainSnapshot(true);
      pending('getOptionsChainSnapshot')[0].flush(chainFailure('Gateway timeout'));
      await refresh;
      fixture.detectChanges();

      expect(text('.exp-asof.stale')).toContain('refresh failed: Gateway timeout');
      expect(fixture.nativeElement.querySelector('.banner.error')).toBeNull();
      expect(component.rows().length).toBe(1);

      const recovery = component.fetchChainSnapshot(true);
      pending('getOptionsChainSnapshot')[0].flush(chainReply(720, 724));
      await recovery;
      fixture.detectChanges();

      expect(fixture.nativeElement.querySelector('.exp-asof.stale')).toBeNull();
    });

    it('does not start a second background refresh while one is still pending', async () => {
      await openPage();

      const first = component.fetchChainSnapshot(true);
      const second = component.fetchChainSnapshot(true);

      const requests = pending('getOptionsChainSnapshot');
      expect(requests.length).toBe(1);
      requests[0].flush(chainReply(720, 725));
      await Promise.all([first, second]);
    });

    it('drops a background reply that lands after the user moved to another ticker', async () => {
      await openPage();
      const poll = component.fetchChainSnapshot(true);
      component.onTickerPicked('QQQ');

      pending('getOptionsChainSnapshot', 'SPY')[0].flush(chainReply(111, 999));
      await poll;
      expect(component.allContracts()).toEqual([]);
      expect(component.underlying()).toBeNull();

      pending('getOptionsExpirations', 'QQQ')[0].flush(expirationsReply([NEAR]));
      pending('getStockSnapshot', 'QQQ')[0].flush(noSnapshot);
      await settle();
      pending('getOptionsChainSnapshot', 'QQQ')[0].flush(chainReply(222, 480));
      await settle();

      expect(component.allContracts().map(c => c.strikePrice)).toEqual([222, 222]);
    });

    it('drops the previous expiry\'s quotes at once, and says so when the new chain fails', async () => {
      await openPage();

      const picking = component.onExpirationSelected(FAR);
      expect(component.allContracts()).toEqual([]);

      pending('getOptionsChainSnapshot')[0].flush(chainFailure('Snapshot unavailable'));
      await picking;
      fixture.detectChanges();

      expect(text('.banner.error')).toContain('Snapshot unavailable');
      expect(fixture.nativeElement.querySelector('.exp-asof')).toBeNull();
    });
  });
});
