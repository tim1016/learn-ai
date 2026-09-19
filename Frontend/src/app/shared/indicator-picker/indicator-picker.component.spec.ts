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

function setup(categories: IndicatorCategory[] = STUB_CATEGORIES): Harness {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({ imports: [IndicatorPickerComponent] });
  const fixture = TestBed.createComponent(IndicatorPickerComponent);
  fixture.componentRef.setInput('categories', categories);
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

  it('facets that filter a non-empty catalog down to nothing keep the filter advice', () => {
    const { fixture, el } = setup();
    // Overlay ∩ momentum is empty: rsi and macd are both sub-panel indicators.
    clickAndFlush(fixture, el.querySelector<HTMLButtonElement>('.ip-chip[data-pane="overlay"]'));
    clickAndFlush(fixture, el.querySelector<HTMLButtonElement>('.ip-chip[data-cat="momentum"]'));
    const empty = textOf(el, '.ip-empty');
    expect(empty).toContain('No indicators match these filters');
    expect(empty).toContain('Try removing a pane or category constraint, or clearing the search.');
    expect(el.querySelector('.ip-link')?.textContent).toContain('Clear search and filters');
  });

  it('an empty catalog says nothing is available instead of blaming filters', () => {
    const { fixture, el } = setup([]);
    const empty = () => textOf(el, '.ip-empty');
    expect(empty()).toContain('No indicators available');
    expect(empty()).not.toContain('No indicators match');
    expect(empty()).not.toContain('Try removing');
    expect(el.querySelector('.ip-link')).toBeNull();

    // A facet toggled on an empty catalog is still not why nothing is listed.
    clickAndFlush(fixture, el.querySelector<HTMLButtonElement>('.ip-chip[data-pane="overlay"]'));
    expect(empty()).toContain('No indicators available');
    expect(empty()).not.toContain('No indicators match');
    expect(el.querySelector('.ip-link')).toBeNull();
  });

  it('shows only the loading state while an empty catalog is loading', () => {
    const { fixture, el } = setup([]);
    fixture.componentRef.setInput('loading', true);
    fixture.detectChanges();
    expect(textOf(el, '.ip-empty')).toContain('Loading indicators…');
    expect(el.textContent).not.toContain('No indicators available');
  });
});

describe('IndicatorPickerComponent search (opt-in)', () => {
  function setupSearchable(): Harness {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ imports: [IndicatorPickerComponent] });
    const fixture = TestBed.createComponent(IndicatorPickerComponent);
    fixture.componentRef.setInput('categories', STUB_CATEGORIES);
    fixture.componentRef.setInput('presets', TEST_PRESETS);
    fixture.componentRef.setInput('searchable', true);
    const add = vi.fn();
    const addInstance = vi.fn();
    const preview = vi.fn();
    fixture.componentInstance.add.subscribe(add);
    fixture.componentInstance.addInstance.subscribe(addInstance);
    fixture.componentInstance.preview.subscribe(preview);
    fixture.detectChanges();
    return { fixture, el: fixture.nativeElement as HTMLElement, add, addInstance, preview };
  }

  function typeSearch(h: Harness, value: string): void {
    const input = h.el.querySelector<HTMLInputElement>('.ip-search-input');
    if (!input) throw new Error('search input not rendered');
    input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    h.fixture.detectChanges();
  }

  function pressKey(h: Harness, key: string): void {
    const input = h.el.querySelector<HTMLInputElement>('.ip-search-input');
    if (!input) throw new Error('search input not rendered');
    input.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }));
    h.fixture.detectChanges();
  }

  it('renders no search input by default (existing consumers unchanged)', () => {
    const { el } = setup();
    expect(el.querySelector('.ip-search-input')).toBeNull();
    expect(el.querySelector('.ip-sr-only')).toBeNull();
  });

  it('renders the search input when searchable and focuses it on init', () => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ imports: [IndicatorPickerComponent] });
    const fixture = TestBed.createComponent(IndicatorPickerComponent);
    fixture.componentRef.setInput('categories', STUB_CATEGORIES);
    fixture.componentRef.setInput('searchable', true);
    // Focus assertions require the element to be in the live document.
    document.body.appendChild(fixture.nativeElement);
    try {
      fixture.detectChanges();
      const input = (fixture.nativeElement as HTMLElement)
        .querySelector<HTMLInputElement>('.ip-search-input');
      expect(input).not.toBeNull();
      expect(document.activeElement).toBe(input);
    } finally {
      (fixture.nativeElement as HTMLElement).remove();
    }
  });

  it('filters by display name, case-insensitively', () => {
    const h = setupSearchable();
    typeSearch(h, 'RSI');
    expect(textOf(h.el, '.ip-count')).toContain('1 of 6');
  });

  it('matches description, category, and parameter-name text', () => {
    const h = setupSearchable();
    typeSearch(h, 'oscillator');      // description of rsi
    expect(textOf(h.el, '.ip-count')).toContain('1 of 6');
    typeSearch(h, 'volatility');      // category name
    expect(textOf(h.el, '.ip-count')).toContain('2 of 6');
    typeSearch(h, 'length');          // configurable parameter name
    expect(textOf(h.el, '.ip-count')).toContain('5 of 6'); // macd has no params
  });

  it('search intersects with the category facet', () => {
    const h = setupSearchable();
    typeSearch(h, 'length');
    clickAndFlush(h.fixture, h.el.querySelector<HTMLButtonElement>('.ip-chip[data-cat="trend"]'));
    expect(textOf(h.el, '.ip-count')).toContain('2 of 6');
  });

  it('announces the result count in an aria-live polite region', () => {
    const h = setupSearchable();
    typeSearch(h, 'rsi');
    const live = h.el.querySelector<HTMLElement>('.ip-sr-only');
    expect(live).not.toBeNull();
    expect(live?.getAttribute('aria-live')).toBe('polite');
    expect(live?.textContent).toContain('1 of 6');
  });

  it('shows an empty state with a combined clear action and clears both search and facets', () => {
    const h = setupSearchable();
    typeSearch(h, 'no-such-indicator');
    expect(textOf(h.el, '.ip-empty')).toContain('No indicators match');
    const clearAll = h.el.querySelector<HTMLButtonElement>('.ip-link');
    expect(clearAll).not.toBeNull();
    clickAndFlush(h.fixture, clearAll);
    expect(textOf(h.el, '.ip-count')).toContain('6 of 6');
    const input = h.el.querySelector<HTMLInputElement>('.ip-search-input');
    expect(input?.value).toBe('');
  });

  it('Clear search button removes only the query, keeping facets', () => {
    const h = setupSearchable();
    clickAndFlush(h.fixture, h.el.querySelector<HTMLButtonElement>('.ip-chip[data-cat="trend"]'));
    typeSearch(h, 'zzz');
    const clearBtn = h.el.querySelector<HTMLButtonElement>('.ip-search-clear');
    expect(clearBtn).not.toBeNull();
    clickAndFlush(h.fixture, clearBtn);
    expect(textOf(h.el, '.ip-count')).toContain('2 of 6'); // trend facet still applied
    expect(h.el.querySelector('.ip-search-clear')).toBeNull();
  });

  it('ArrowDown/ArrowUp/Home/End move the active option highlight through results', () => {
    const h = setupSearchable();
    typeSearch(h, 'length'); // 5 results; flat order: ema, sma, rsi, bbands, atr
    clickAndFlush(h.fixture, h.el.querySelector<HTMLButtonElement>('.ip-cat[data-cat="trend"] .ip-cat-head'));
    clickAndFlush(h.fixture, h.el.querySelector<HTMLButtonElement>('.ip-cat[data-cat="momentum"] .ip-cat-head'));
    clickAndFlush(h.fixture, h.el.querySelector<HTMLButtonElement>('.ip-cat[data-cat="volatility"] .ip-cat-head'));
    const activeRow = () => h.el.querySelector<HTMLElement>('.ip-row--active-option');
    expect(activeRow()).toBeNull();
    pressKey(h, 'ArrowDown');
    expect(activeRow()?.getAttribute('data-name')).toBe('ema');
    pressKey(h, 'ArrowDown');
    expect(activeRow()?.getAttribute('data-name')).toBe('sma');
    pressKey(h, 'End');
    expect(activeRow()?.getAttribute('data-name')).toBe('atr');
    pressKey(h, 'Home');
    expect(activeRow()?.getAttribute('data-name')).toBe('ema');
    pressKey(h, 'ArrowUp');
    expect(activeRow()?.getAttribute('data-name')).toBe('atr'); // wraps
  });

  it('Enter adds the active option with default params', () => {
    const h = setupSearchable();
    typeSearch(h, 'bollinger');
    pressKey(h, 'ArrowDown');
    pressKey(h, 'Enter');
    expect(h.add).toHaveBeenCalledWith({ name: 'bbands', params: { length: 20 } });
  });

  it('Escape clears the search first and keeps the facet state intact', () => {
    const h = setupSearchable();
    clickAndFlush(h.fixture, h.el.querySelector<HTMLButtonElement>('.ip-chip[data-cat="trend"]'));
    typeSearch(h, 'zzz');
    pressKey(h, 'Escape');
    expect(h.el.querySelector<HTMLInputElement>('.ip-search-input')?.value).toBe('');
    expect(textOf(h.el, '.ip-count')).toContain('2 of 6'); // facet survived
  });

  it('keyboard navigation only reaches rendered rows — closed categories are skipped', () => {
    const h = setupSearchable();
    // Open trend and volatility but leave momentum closed. Flat order of
    // RENDERED rows: ema, sma (trend), bbands, atr (volatility) — rsi/macd
    // are invisible and must never take the highlight.
    clickAndFlush(h.fixture, h.el.querySelector<HTMLButtonElement>('.ip-cat[data-cat="trend"] .ip-cat-head'));
    clickAndFlush(h.fixture, h.el.querySelector<HTMLButtonElement>('.ip-cat[data-cat="volatility"] .ip-cat-head'));
    const activeRow = () => h.el.querySelector<HTMLElement>('.ip-row--active-option');
    pressKey(h, 'ArrowDown');
    expect(activeRow()?.getAttribute('data-name')).toBe('ema');
    pressKey(h, 'ArrowDown');
    expect(activeRow()?.getAttribute('data-name')).toBe('sma');
    pressKey(h, 'ArrowDown');
    expect(activeRow()?.getAttribute('data-name')).toBe('bbands');
    pressKey(h, 'ArrowDown');
    expect(activeRow()?.getAttribute('data-name')).toBe('atr');
    pressKey(h, 'ArrowDown'); // wraps within the rendered rows only
    expect(activeRow()?.getAttribute('data-name')).toBe('ema');
  });

  it('search opens matching categories so every keyboard option is rendered', () => {
    const h = setupSearchable();
    typeSearch(h, 'length'); // matches ema, sma, rsi, bbands, atr — no cats open
    const activeRow = () => h.el.querySelector<HTMLElement>('.ip-row--active-option');
    pressKey(h, 'ArrowDown');
    expect(activeRow()?.getAttribute('data-name')).toBe('ema');
    pressKey(h, 'ArrowDown');
    pressKey(h, 'ArrowDown');
    // rsi sits in momentum, which no one opened manually — search opened it.
    expect(activeRow()?.getAttribute('data-name')).toBe('rsi');
  });

  it('exposes the combobox/listbox relationship — the input announces the Enter target', () => {
    const h = setupSearchable();
    const input = h.el.querySelector<HTMLInputElement>('.ip-search-input');
    if (!input) throw new Error('search input not rendered');
    expect(input.getAttribute('role')).toBe('combobox');
    expect(input.getAttribute('aria-expanded')).toBe('false');
    expect(input.getAttribute('aria-controls')).toBe('ip-rows');
    expect(input.getAttribute('aria-activedescendant')).toBeNull();

    typeSearch(h, 'length'); // 5 matches — matching categories render open
    expect(input.getAttribute('aria-expanded')).toBe('true');
    pressKey(h, 'ArrowDown');
    const row = h.el.querySelector<HTMLElement>('.ip-row--active-option');
    expect(row?.getAttribute('role')).toBe('option');
    expect(row?.id).toBe('ip-opt-ema');
    expect(row?.getAttribute('aria-selected')).toBe('true');
    expect(input.getAttribute('aria-activedescendant')).toBe('ip-opt-ema');
    // Non-active rows are unselected options.
    const unselected = h.el.querySelector<HTMLElement>('.ip-row:not(.ip-row--active-option)');
    expect(unselected?.getAttribute('aria-selected')).toBe('false');
  });
});
