import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';
import type {
  ExpirationsResponse,
  IbkrStrikeList,
  OptionContractMatch,
  OptionContractsResponse,
  SymbolMatch,
} from '../../../../../api/broker-models';
import { BrokerService } from '../../../../../services/broker.service';
import { OptionLegPickerComponent } from './option-leg-picker.component';

const SPY: SymbolMatch = {
  symbol: 'SPY',
  name: 'SPDR S&P 500 ETF Trust',
  exchange: 'ARCA',
  currency: 'USD',
  sec_type: 'STK',
  derivative_sec_types: ['OPT'],
};

const QUALIFIED: OptionContractMatch = {
  con_id: 42,
  symbol: 'SPY',
  local_symbol: 'SPY   251219C00650000',
  trading_class: 'SPY',
  exchange: 'SMART',
  currency: 'USD',
  expiry_ms: 1_766_188_800_000,
  strike: 650.0,
  right: 'C',
  multiplier: 100,
};

function setup(opts: {
  expirations?: number[];
  strikes?: number[];
  qualifyResult?: OptionContractsResponse;
  qualifyThrows?: unknown;
} = {}) {
  TestBed.resetTestingModule();
  const broker = {
    expirations: vi.fn(
      async (): Promise<ExpirationsResponse> => ({
        symbol: 'SPY',
        expirations_ms: opts.expirations ?? [1_766_188_800_000, 1_768_780_800_000],
      }),
    ),
    strikes: vi.fn(
      async (): Promise<IbkrStrikeList> => ({
        symbol: 'SPY',
        expiry_ms: 1_766_188_800_000,
        strikes: opts.strikes ?? [640, 645, 650, 655, 660],
        fetched_at_ms: 0,
      }),
    ),
    searchOptionContracts: vi.fn(async (): Promise<OptionContractsResponse> => {
      if (opts.qualifyThrows !== undefined) throw opts.qualifyThrows;
      return opts.qualifyResult ?? { matches: [QUALIFIED] };
    }),
  };

  TestBed.configureTestingModule({
    providers: [
      provideZonelessChangeDetection(),
      { provide: BrokerService, useValue: broker },
    ],
  });
  const fixture = TestBed.createComponent(OptionLegPickerComponent);
  fixture.componentRef.setInput('symbol', SPY);
  fixture.detectChanges();
  return {
    fixture,
    component: fixture.componentInstance,
    el: fixture.nativeElement as HTMLElement,
    broker,
  };
}

async function flushAsync() {
  await Promise.resolve();
  await Promise.resolve();
}

describe('OptionLegPickerComponent', () => {
  it('loads expirations on symbol change and renders them as chips', async () => {
    const { fixture, broker, el } = setup();

    await flushAsync();
    fixture.detectChanges();

    expect(broker.expirations).toHaveBeenCalledWith('SPY');
    expect(el.querySelectorAll('.chip').length).toBeGreaterThan(0);
  });

  it('drill-down: expiry → strike → right → qualify emits the contract', async () => {
    const { fixture, component, broker } = setup();
    const onQualify = vi.fn();
    component.qualify.subscribe(onQualify);

    await flushAsync();
    fixture.detectChanges();

    component.selectExpiry(1_766_188_800_000);
    await flushAsync();
    fixture.detectChanges();

    component.selectStrike(650);
    fixture.detectChanges();

    expect(component.canQualify()).toBe(true);

    component.selectRight('C');
    await component.qualifyContract();

    expect(broker.searchOptionContracts).toHaveBeenCalledWith(
      'SPY',
      1_766_188_800_000,
      650,
      'C',
    );
    expect(onQualify).toHaveBeenCalledWith(QUALIFIED);
  });

  it('shows error when qualification returns no match', async () => {
    const { fixture, component } = setup({ qualifyResult: { matches: [] } });

    await flushAsync();
    fixture.detectChanges();
    component.selectExpiry(1_766_188_800_000);
    await flushAsync();
    component.selectStrike(650);
    await component.qualifyContract();
    fixture.detectChanges();

    expect(component.error()).toMatch(/could not qualify/i);
  });

  it('surfaces 503 as a reconnect prompt', async () => {
    const { fixture, component } = setup({ qualifyThrows: { status: 503 } });

    await flushAsync();
    fixture.detectChanges();
    component.selectExpiry(1_766_188_800_000);
    await flushAsync();
    component.selectStrike(650);
    await component.qualifyContract();

    expect(component.error()).toMatch(/IBKR offline/i);
  });

  it('resets drill-down when the symbol changes', async () => {
    const { fixture, component } = setup();

    await flushAsync();
    fixture.detectChanges();
    component.selectExpiry(1_766_188_800_000);
    component.selectStrike(650);
    fixture.detectChanges();

    const QQQ: SymbolMatch = { ...SPY, symbol: 'QQQ', exchange: 'NASDAQ' };
    fixture.componentRef.setInput('symbol', QQQ);
    await flushAsync();
    fixture.detectChanges();

    expect(component.selectedExpiryMs()).toBeNull();
    expect(component.selectedStrike()).toBeNull();
  });
});
