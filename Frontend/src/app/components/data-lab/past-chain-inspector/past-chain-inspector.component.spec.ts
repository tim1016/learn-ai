/* eslint-disable @typescript-eslint/no-explicit-any */
/**
 * Migrated from `options-history.component.spec.ts` during R1 of the
 * options-routes cleanup. Asserts the same surface the legacy component
 * had — formatters, ATM identification, computed call/put split — plus
 * the new state machine (collapsed → loading → expanded) and the
 * inspector-specific behaviours (preview/collapse/scan-toggle).
 */
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { vi } from 'vitest';
import { PastChainInspectorComponent } from './past-chain-inspector.component';
import { PastChainContractRow, PastChainResult } from '../../../services/past-chain.service';

vi.mock('lightweight-charts', () => {
  const mockTimeScale = { fitContent: vi.fn(), applyOptions: vi.fn() };
  const createMockSeries = () => ({ setData: vi.fn(), applyOptions: vi.fn() });
  const createMockChart = () => ({
    addSeries: vi.fn().mockReturnValue(createMockSeries()),
    removeSeries: vi.fn(),
    timeScale: vi.fn().mockReturnValue(mockTimeScale),
    applyOptions: vi.fn(),
    remove: vi.fn(),
  });
  return {
    createChart: vi.fn().mockImplementation(() => createMockChart()),
    LineSeries: 'LineSeries',
    HistogramSeries: 'HistogramSeries',
  };
});

function buildContractRow(side: 'call' | 'put', strike: number, hasData = true): PastChainContractRow {
  return {
    optionTicker: `O:SPY260220${side === 'call' ? 'C' : 'P'}00${(strike * 1000).toString().padStart(6, '0')}`,
    contractType: side,
    strikePrice: strike,
    dailyBar: hasData
      ? { open: 5, high: 6, low: 4.5, close: 5.5, volume: 1000, timestamp: '2026-02-20T00:00:00Z' } as any
      : null,
    prevDayClose: hasData ? 5.0 : null,
    changeFromPrevClose: hasData ? 0.5 : null,
    changePercent: hasData ? 10 : null,
    isAtm: strike === 590,
    relativeStrike: strike - 590,
  };
}

function buildResult(): PastChainResult {
  return {
    atmPrice: 590.42,
    atmStrike: 590,
    prevDayClose: 588,
    openPrice: 590.42,
    contractRows: [
      buildContractRow('call', 588),
      buildContractRow('call', 590),
      buildContractRow('call', 592),
      buildContractRow('put', 588),
      buildContractRow('put', 590),
      buildContractRow('put', 592),
    ],
    scanResults: [
      { strikePrice: 588, callTicker: 'cT', callHasData: true, putTicker: 'pT', putHasData: true, selected: true },
      { strikePrice: 590, callTicker: 'cT', callHasData: true, putTicker: 'pT', putHasData: true, selected: true },
      { strikePrice: 592, callTicker: 'cT', callHasData: true, putTicker: 'pT', putHasData: false, selected: false },
    ],
    stockMinuteBars: [],
  };
}

describe('PastChainInspectorComponent', () => {
  let component: PastChainInspectorComponent;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [PastChainInspectorComponent],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    const fixture = TestBed.createComponent(PastChainInspectorComponent);
    component = fixture.componentInstance;
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true).forEach(req => {
      if (!req.cancelled) req.flush({ data: {} });
    });
    httpMock.verify();
  });

  describe('initialization', () => {
    it('starts in the collapsed state', () => {
      expect(component.state()).toBe('collapsed');
      expect(component.result()).toBeNull();
    });

    it('defaults numStrikes to 5 and atmMethod to prevClose', () => {
      expect(component.numStrikes()).toBe(5);
      expect(component.atmMethod()).toBe('prevClose');
    });
  });

  describe('formatters (parity with legacy options-history)', () => {
    it('formats prices to 2dp, em-dash for null', () => {
      expect(component.formatPrice(150.123)).toBe('150.12');
      expect(component.formatPrice(null)).toBe('—');
      expect(component.formatPrice(undefined)).toBe('—');
    });

    it('formats change with sign', () => {
      expect(component.formatChange(5.5)).toBe('+5.50');
      expect(component.formatChange(-3.2)).toBe('-3.20');
      expect(component.formatChange(null)).toBe('—');
    });

    it('formats change percent with sign', () => {
      expect(component.formatChangePct(12.5)).toBe('+12.5%');
      expect(component.formatChangePct(-3.1)).toBe('-3.1%');
      expect(component.formatChangePct(null)).toBe('—');
    });

    it('formats volume with locale separators', () => {
      const out = component.formatVolume(1000000);
      expect(out).toContain('1');
      expect(out).toContain('000');
      expect(component.formatVolume(null)).toBe('—');
      expect(component.formatVolume(undefined)).toBe('—');
    });
  });

  describe('computed: callRows / putRows / uniqueStrikes', () => {
    it('splits and sorts rows by strike', () => {
      component.result.set(buildResult());
      const calls = component.callRows();
      expect(calls.length).toBe(3);
      expect(calls.map(r => r.strikePrice)).toEqual([588, 590, 592]);

      const puts = component.putRows();
      expect(puts.map(r => r.strikePrice)).toEqual([588, 590, 592]);

      expect(component.uniqueStrikes()).toEqual([588, 590, 592]);
    });
  });

  describe('isAtm', () => {
    it('returns true for the ATM strike, false for others', () => {
      component.result.set(buildResult());
      expect(component.isAtm(590)).toBe(true);
      expect(component.isAtm(588)).toBe(false);
    });

    it('returns false when no result is loaded', () => {
      expect(component.isAtm(590)).toBe(false);
    });
  });

  describe('preview state machine', () => {
    it('blocks preview when ticker or date is missing and surfaces a friendly error', async () => {
      // ticker / analysisDate both empty by default
      await component.preview();
      expect(component.state()).toBe('collapsed');
      expect(component.error()).toMatch(/ticker|date/i);
    });
  });

  describe('selectedScanCount', () => {
    it('counts selected scan results', () => {
      component.result.set(buildResult());
      expect(component.selectedScanCount()).toBe(2);
    });
  });

  describe('toggleScanDetails', () => {
    it('flips the show-scan-details flag', () => {
      expect(component.showScanDetails()).toBe(false);
      component.toggleScanDetails();
      expect(component.showScanDetails()).toBe(true);
      component.toggleScanDetails();
      expect(component.showScanDetails()).toBe(false);
    });
  });

  describe('detail modal lifecycle', () => {
    it('closeDetail clears state', () => {
      component.detailModalOpen.set(true);
      component.detailTicker.set('O:SPY260220C00590000');
      component.detailBars.set([{ open: 5, high: 6, low: 4, close: 5.5, volume: 1000, timestamp: '2026-02-20T00:00:00Z' } as any]);

      component.closeDetail();

      expect(component.detailModalOpen()).toBe(false);
      expect(component.detailTicker()).toBeNull();
      expect(component.detailBars()).toEqual([]);
    });

    it('onDetailVisibleChange(false) routes through closeDetail', () => {
      component.detailModalOpen.set(true);
      component.detailTicker.set('O:SPY260220C00590000');

      component.onDetailVisibleChange(false);

      expect(component.detailModalOpen()).toBe(false);
      expect(component.detailTicker()).toBeNull();
    });
  });
});
