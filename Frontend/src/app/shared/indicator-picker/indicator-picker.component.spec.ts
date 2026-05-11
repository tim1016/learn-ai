import { ComponentFixture, TestBed } from '@angular/core/testing';
import { vi } from 'vitest';

import { IndicatorCategory } from '../indicator-catalog/indicator-catalog.service';
import { IndicatorPickerAdd, IndicatorPickerComponent } from './indicator-picker.component';
import { IndicatorPreset } from './indicator-picker.presets';

const STUB_CATEGORIES: IndicatorCategory[] = [
  {
    name: 'trend',
    indicators: [
      { name: 'ema',  category: 'trend', description: 'Exponential MA.', configurable_params: [{ name: 'length', type: 'int', default: 10, min: 2, max: 200, description: 'Window length.' }] },
      { name: 'sma',  category: 'trend', description: 'Simple MA.',      configurable_params: [{ name: 'length', type: 'int', default: 10, min: 2, max: 200, description: 'Window length.' }] },
    ],
  },
  {
    name: 'momentum',
    indicators: [
      { name: 'rsi',  category: 'momentum', description: 'RSI oscillator.', configurable_params: [{ name: 'length', type: 'int', default: 14, min: 2, max: 200, description: 'Window length.' }] },
      { name: 'macd', category: 'momentum', description: 'MACD.',           configurable_params: [] },
    ],
  },
  {
    name: 'volatility',
    indicators: [
      { name: 'bbands', category: 'volatility', description: 'Bollinger.', configurable_params: [{ name: 'length', type: 'int', default: 20, min: 2, max: 200, description: 'Window length.' }] },
      { name: 'atr',    category: 'volatility', description: 'ATR.',       configurable_params: [{ name: 'length', type: 'int', default: 14, min: 2, max: 200, description: 'Window length.' }] },
    ],
  },
];

const TEST_PRESETS: IndicatorPreset[] = [
  {
    name: 'EMA ribbon', subtitle: '5/10/20/50', stack: 's4', count: '×4',
    instances: [
      { indicator: 'ema', params: { length: 5 } },
      { indicator: 'ema', params: { length: 10 } },
      { indicator: 'ema', params: { length: 20 } },
      { indicator: 'ema', params: { length: 50 } },
    ],
  },
];

interface Harness {
  fixture: ComponentFixture<IndicatorPickerComponent>;
  el: HTMLElement;
  add: ReturnType<typeof vi.fn>;
  addInstance: ReturnType<typeof vi.fn>;
  preview: ReturnType<typeof vi.fn>;
}

function setup(): Harness {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({ imports: [IndicatorPickerComponent] });
  const fixture = TestBed.createComponent(IndicatorPickerComponent);
  fixture.componentRef.setInput('categories', STUB_CATEGORIES);
  fixture.componentRef.setInput('presets', TEST_PRESETS);
  const add = vi.fn();
  const addInstance = vi.fn();
  const preview = vi.fn();
  fixture.componentInstance.add.subscribe(add);
  fixture.componentInstance.addInstance.subscribe(addInstance);
  fixture.componentInstance.preview.subscribe(preview);
  fixture.detectChanges();
  return { fixture, el: fixture.nativeElement as HTMLElement, add, addInstance, preview };
}

function clickAndFlush(fixture: ComponentFixture<unknown>, el: HTMLElement | null): void {
  if (!el) throw new Error('clickAndFlush: element is null');
  el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
  fixture.detectChanges();
}

function findRow(host: HTMLElement, name: string): HTMLElement {
  const row = host.querySelector<HTMLElement>(`.ip-row[data-name="${name}"]`);
  if (!row) throw new Error(`row ${name} not rendered (is its category open?)`);
  return row;
}

function textOf(host: HTMLElement, selector: string): string {
  const el = host.querySelector(selector);
  if (!el) throw new Error(`element ${selector} not found`);
  return el.textContent ?? '';
}

describe('IndicatorPickerComponent', () => {
  it('renders the eyebrow header with the total catalog count', () => {
    const { el } = setup();
    expect(textOf(el, '.ip-eyebrow')).toContain('Indicators');
    expect(textOf(el, '.ip-count')).toContain('6 of 6');
  });

  it('renders presets and emits one addInstance per instance when clicked', () => {
    const { fixture, el, addInstance } = setup();
    const preset = el.querySelector<HTMLButtonElement>('.ip-preset');
    expect(preset).not.toBeNull();
    clickAndFlush(fixture, preset);
    expect(addInstance).toHaveBeenCalledTimes(4);
    expect(addInstance.mock.calls.map(c => (c[0] as IndicatorPickerAdd).params['length'])).toEqual([5, 10, 20, 50]);
  });

  it('Category chip filters the visible set to only that category', () => {
    const { fixture, el } = setup();
    clickAndFlush(fixture, el.querySelector<HTMLButtonElement>('.ip-chip[data-cat="momentum"]'));
    expect(textOf(el, '.ip-count')).toContain('2 of 6');
  });

  it('Pane chip filters orthogonally — Overlay-only leaves overlay indicators visible', () => {
    const { fixture, el } = setup();
    clickAndFlush(fixture, el.querySelector<HTMLButtonElement>('.ip-chip[data-pane="overlay"]'));
    // Overlay names in stub: ema, sma, bbands → 3 of 6
    expect(textOf(el, '.ip-count')).toContain('3 of 6');
  });

  it('opening a category lists its rows; Add emits with default params', () => {
    const { fixture, el, add } = setup();
    clickAndFlush(fixture, el.querySelector<HTMLButtonElement>('.ip-cat[data-cat="trend"] .ip-cat-head'));
    const emaRow = findRow(el, 'ema');
    clickAndFlush(fixture, emaRow.querySelector<HTMLButtonElement>('.ip-btn:not(.iconic)'));
    expect(add).toHaveBeenCalledWith({ name: 'ema', params: { length: 10 } });
  });

  it('+N badge reflects activeKeys count for a row', () => {
    const { fixture, el } = setup();
    fixture.componentRef.setInput('activeKeys', ['ema', 'ema', 'ema']);
    fixture.detectChanges();
    clickAndFlush(fixture, el.querySelector<HTMLButtonElement>('.ip-cat[data-cat="trend"] .ip-cat-head'));
    const emaRow = findRow(el, 'ema');
    expect(textOf(emaRow, '.ip-instances')).toBe('+3');
  });

  it('emits (preview) with active=true after a 300ms hover, false on leave', async () => {
    vi.useFakeTimers();
    try {
      const { fixture, el, preview } = setup();
      clickAndFlush(fixture, el.querySelector<HTMLButtonElement>('.ip-cat[data-cat="trend"] .ip-cat-head'));
      const row = findRow(el, 'ema');
      row.dispatchEvent(new MouseEvent('mouseenter'));
      vi.advanceTimersByTime(299);
      expect(preview).not.toHaveBeenCalled();
      vi.advanceTimersByTime(1);
      fixture.detectChanges();
      expect(preview).toHaveBeenCalledWith({ name: 'ema', active: true });
      row.dispatchEvent(new MouseEvent('mouseleave'));
      fixture.detectChanges();
      expect(preview).toHaveBeenLastCalledWith({ name: 'ema', active: false });
    } finally {
      vi.useRealTimers();
    }
  });

  it('clear filters button appears only when a facet is active and resets state', () => {
    const { fixture, el } = setup();
    expect(el.querySelector('.ip-chip-clear')).toBeNull();
    clickAndFlush(fixture, el.querySelector<HTMLButtonElement>('.ip-chip[data-pane="overlay"]'));
    const clear = el.querySelector<HTMLButtonElement>('.ip-chip-clear');
    expect(clear).not.toBeNull();
    clickAndFlush(fixture, clear);
    expect(el.querySelector('.ip-chip-clear')).toBeNull();
  });
});
