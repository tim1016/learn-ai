import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { OptionsLabChainComponent } from './options-lab-chain.component';
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
    expirationDate: '2026-05-15',
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
  let httpMock: HttpTestingController;

  beforeEach(() => {
    localStorage.clear();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [OptionsLabChainComponent],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    const fixture = TestBed.createComponent(OptionsLabChainComponent);
    component = fixture.componentInstance;
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    drainAll(httpMock);
    httpMock.verify();
  });

  it('creates the component with default state', () => {
    expect(component).toBeTruthy();
    expect(component.ticker()).toBe('SPY');
    expect(component.density()).toBe('standard');
    expect(component.strikeRange()).toBe(15);
    expect(component.showAllStrikes()).toBe(false);
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
    expect(component.density()).toBe('standard');
    component.toggleDensity();
    expect(component.density()).toBe('greeks');
    expect(localStorage.getItem('optionsLab.chain.density')).toBe('greeks');
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
});
