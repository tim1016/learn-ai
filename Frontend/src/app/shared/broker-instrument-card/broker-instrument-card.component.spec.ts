import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed, type ComponentFixture } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { SymbolMatch, SymbolSearchResponse } from '../../api/broker-models';
import { BrokerService } from '../../services/broker.service';
import { BrokerInstrumentCardComponent } from './broker-instrument-card.component';

const SPY: SymbolMatch = {
  symbol: 'SPY',
  name: 'SPDR S&P 500 ETF Trust',
  exchange: 'ARCA',
  currency: 'USD',
  sec_type: 'STK',
  derivative_sec_types: ['OPT'],
};

const QQQ: SymbolMatch = {
  symbol: 'QQQ',
  name: 'Invesco QQQ Trust',
  exchange: 'NASDAQ',
  currency: 'USD',
  sec_type: 'STK',
  derivative_sec_types: ['OPT'],
};

interface FakeBrokerService {
  searchSymbols: ReturnType<typeof vi.fn>;
}

function setup(searchImpl: (q: string) => Promise<SymbolSearchResponse>) {
  TestBed.resetTestingModule();
  const broker: FakeBrokerService = { searchSymbols: vi.fn(searchImpl) };
  TestBed.configureTestingModule({
    providers: [
      provideZonelessChangeDetection(),
      { provide: BrokerService, useValue: broker },
    ],
  });
  const fixture = TestBed.createComponent(BrokerInstrumentCardComponent);
  fixture.detectChanges();
  return {
    fixture,
    component: fixture.componentInstance,
    el: fixture.nativeElement as HTMLElement,
    broker,
  };
}

async function flushDebounce(fixture: ComponentFixture<BrokerInstrumentCardComponent>) {
  await vi.advanceTimersByTimeAsync(550);
  await Promise.resolve();
  fixture.detectChanges();
  await Promise.resolve();
  fixture.detectChanges();
}

describe('BrokerInstrumentCardComponent', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('debounces typed queries and renders broker matches', async () => {
    const { fixture, component, el, broker } = setup(async () => ({
      matches: [SPY, QQQ],
    }));

    component.onQueryChange('SP');
    fixture.detectChanges();
    expect(broker.searchSymbols).not.toHaveBeenCalled();

    await flushDebounce(fixture);

    expect(broker.searchSymbols).toHaveBeenCalledTimes(1);
    expect(broker.searchSymbols).toHaveBeenCalledWith('SP', undefined);
    expect(el.textContent).toContain('SPY');
    expect(el.textContent).toContain('Invesco QQQ Trust');
  });

  it('emits the picked match and collapses the dropdown', async () => {
    const { fixture, component, el } = setup(async () => ({ matches: [SPY] }));
    const onPick = vi.fn();
    component.pick.subscribe(onPick);

    component.onQueryChange('SPY');
    await flushDebounce(fixture);

    const button = el.querySelector<HTMLButtonElement>('.match-button');
    if (button === null) throw new Error('expected .match-button to render');
    button.click();
    fixture.detectChanges();

    expect(onPick).toHaveBeenCalledWith(SPY);
    expect(el.querySelector('.match-button')).toBeNull();
  });

  it('shows reconnect prompt when broker returns 503', async () => {
    const { fixture, component, el } = setup(async () => {
      throw { status: 503 };
    });

    component.onQueryChange('SPY');
    await flushDebounce(fixture);

    const alert = el.querySelector<HTMLElement>('[role="alert"]');
    if (alert === null) throw new Error('expected [role="alert"] to render');
    expect(alert.textContent).toMatch(/Reconnect broker/i);
  });

  it('shows a rate-limit warning on 429', async () => {
    const { fixture, component, el } = setup(async () => {
      throw { status: 429 };
    });

    component.onQueryChange('SPY');
    await flushDebounce(fixture);

    const alert = el.querySelector<HTMLElement>('[role="alert"]');
    if (alert === null) throw new Error('expected [role="alert"] to render');
    expect(alert.textContent).toMatch(/too fast|wait/i);
  });

  it('clears matches when the query is blanked', async () => {
    const { fixture, component, el } = setup(async () => ({ matches: [SPY] }));

    component.onQueryChange('SPY');
    await flushDebounce(fixture);
    expect(el.querySelector('.match-button')).not.toBeNull();

    component.onQueryChange('');
    await flushDebounce(fixture);

    expect(el.querySelector('.match-button')).toBeNull();
  });
});
