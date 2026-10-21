import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
  type TestRequest,
} from '@angular/common/http/testing';
import { OptionsLabChainComponent } from './options-lab-chain.component';
import { fakePickerWorld } from '../../../shared/symbol-picker/testing/fake-picker-world';
import { SnapshotContractResult } from '../../../services/market-data.types';
import type { components } from '../../../api/broker.types';
import { environment } from '../../../../environments/environment';

type Schema = components['schemas'];

const API = `${environment.pythonServiceUrl}/api`;

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

/** A contract in the wire shape, `makeContract`'s twin. */
function wireContract(contractType: 'call' | 'put', strike: number): NonNullable<Schema['OptionsChainSnapshotResponse']['contracts']>[number] {
  return {
    ticker: `O:SPY${strike}${contractType[0].toUpperCase()}`,
    contract_type: contractType,
    strike_price: strike,
    expiration_ms: 1_778_875_200_000,
    break_even_price: null,
    implied_volatility: 0.2,
    open_interest: 100,
    greeks: { delta: 0.5, gamma: 0.01, theta: -0.05, vega: 0.1 },
    day: { open: 1, high: 1, low: 1, close: 1, volume: 1000, vwap: 1 },
    last_trade: null,
    last_quote: null,
  };
}

function drainAll(httpMock: HttpTestingController): void {
  httpMock.match(() => true).forEach(req => {
    if (!req.cancelled) req.flush({});
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

    const req = httpMock.expectOne(`${API}/snapshot/options-chain`);
    req.flush({
      success: true,
      underlying: { ticker: 'SPY', price: 723.77, change: 5.76, change_percent: 0.8 },
      contracts: [],
      count: 0,
      risk_free_rate: null,
      dividend_yield: null,
      rate_source: null,
      dividend_source: null,
      error: null,
    } satisfies Schema['OptionsChainSnapshotResponse']);

    await fetchPromise;
    expect(component.error()).toBeNull();
  });

  // ── Failures and stale data are shown, not hidden (#3221) ──
  describe('failures and stale quotes', () => {
    const NEAR = 4_070_984_400_000;
    const FAR = 4_073_576_400_000;

    /** The pending requests to a Python route (`options/expirations`, ...), for one ticker when given. */
    function pending(route: string, ticker?: string): TestRequest[] {
      return httpMock.match(r => {
        const b = r.body as { underlying_ticker?: string; ticker?: string } | null;
        return r.url === `${API}/${route}`
          && (ticker === undefined || (b?.underlying_ticker ?? b?.ticker) === ticker);
      });
    }

    function chainReply(strike: number, price: number): Schema['OptionsChainSnapshotResponse'] {
      return {
        success: true,
        underlying: { ticker: 'SPY', price, change: 0, change_percent: 0 },
        contracts: [wireContract('call', strike), wireContract('put', strike)],
        count: 2, risk_free_rate: null, dividend_yield: null, rate_source: null, dividend_source: null, error: null,
      };
    }

    /** Python's answer to a call it could not serve: an HTTP error and a `detail`. */
    function failWith(request: TestRequest, detail: string): void {
      request.flush({ detail }, { status: 500, statusText: 'Internal Server Error' });
    }

    function expirationsReply(expirations: number[]): Schema['OptionsExpirationsResponse'] {
      return { success: true, expirations, count: expirations.length, error: null };
    }

    const noSnapshot: Schema['StockSnapshotResponse'] = { success: false, snapshot: null, error: null };

    async function settle(): Promise<void> {
      await new Promise(resolve => setTimeout(resolve, 0));
      fixture.detectChanges();
    }

    /** Render the page and let its first expirations + chain load finish. */
    async function openPage(): Promise<void> {
      fixture.detectChanges();
      pending('options/expirations')[0].flush(expirationsReply([NEAR, FAR]));
      pending('snapshot/ticker')[0].flush(noSnapshot);
      await settle();
      pending('snapshot/options-chain')[0].flush(chainReply(720, 723.77));
      await settle();
    }

    function text(selector: string): string | null {
      return fixture.nativeElement.querySelector(selector)?.textContent?.replace(/\s+/g, ' ').trim() ?? null;
    }

    it('shows the reason when the expirations lookup fails, not just an empty chain', async () => {
      fixture.detectChanges();
      failWith(pending('options/expirations')[0], 'Failed to list options expirations: Polygon rate limited');
      pending('snapshot/ticker')[0].flush(noSnapshot);
      await settle();

      expect(text('.banner.error')).toContain('Polygon rate limited');
    });

    it('marks the quotes stale, with the reason, when a background refresh fails', async () => {
      await openPage();
      expect(text('.exp-asof')).toContain('Quotes as of');
      expect(fixture.nativeElement.querySelector('.exp-asof.stale')).toBeNull();

      const refresh = component.fetchChainSnapshot(true);
      failWith(pending('snapshot/options-chain')[0], 'Gateway timeout');
      await refresh;
      fixture.detectChanges();

      expect(text('.exp-asof.stale')).toContain('refresh failed: Gateway timeout');
      expect(fixture.nativeElement.querySelector('.banner.error')).toBeNull();
      expect(component.rows().length).toBe(1);

      const recovery = component.fetchChainSnapshot(true);
      pending('snapshot/options-chain')[0].flush(chainReply(720, 724));
      await recovery;
      fixture.detectChanges();

      expect(fixture.nativeElement.querySelector('.exp-asof.stale')).toBeNull();
    });

    it('does not start a second background refresh while one is still pending', async () => {
      await openPage();

      const first = component.fetchChainSnapshot(true);
      const second = component.fetchChainSnapshot(true);

      const requests = pending('snapshot/options-chain');
      expect(requests.length).toBe(1);
      requests[0].flush(chainReply(720, 725));
      await Promise.all([first, second]);
    });

    it('drops a background reply that lands after the user moved to another ticker', async () => {
      await openPage();
      const poll = component.fetchChainSnapshot(true);
      component.onTickerPicked('QQQ');

      pending('snapshot/options-chain', 'SPY')[0].flush(chainReply(111, 999));
      await poll;
      expect(component.allContracts()).toEqual([]);
      expect(component.underlying()).toBeNull();

      pending('options/expirations', 'QQQ')[0].flush(expirationsReply([NEAR]));
      pending('snapshot/ticker', 'QQQ')[0].flush(noSnapshot);
      await settle();
      pending('snapshot/options-chain', 'QQQ')[0].flush(chainReply(222, 480));
      await settle();

      expect(component.allContracts().map(c => c.strikePrice)).toEqual([222, 222]);
    });

    it('drops the previous expiry\'s quotes at once, and says so when the new chain fails', async () => {
      await openPage();

      const picking = component.onExpirationSelected(FAR);
      expect(component.allContracts()).toEqual([]);

      failWith(pending('snapshot/options-chain')[0], 'Snapshot unavailable');
      await picking;
      fixture.detectChanges();

      expect(text('.banner.error')).toContain('Snapshot unavailable');
      expect(fixture.nativeElement.querySelector('.exp-asof')).toBeNull();
    });
  });
});
