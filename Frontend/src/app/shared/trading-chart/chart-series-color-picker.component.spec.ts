import { ComponentFixture, TestBed } from '@angular/core/testing';
import { vi } from 'vitest';

import {
  ChartSeriesColorPickerComponent,
} from './chart-series-color-picker.component';
import { ChartSeriesColorToken } from './chart-series-color-tokens';

interface Harness {
  fixture: ComponentFixture<ChartSeriesColorPickerComponent>;
  el: HTMLElement;
  tokenSelected: ReturnType<typeof vi.fn>;
}

function setup(selected: ChartSeriesColorToken | null = null): Harness {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({ imports: [ChartSeriesColorPickerComponent] });
  const fixture = TestBed.createComponent(ChartSeriesColorPickerComponent);
  fixture.componentRef.setInput('selected', selected);
  const tokenSelected = vi.fn();
  fixture.componentInstance.tokenSelected.subscribe(tokenSelected);
  fixture.detectChanges();
  return { fixture, el: fixture.nativeElement as HTMLElement, tokenSelected };
}

function radioFor(host: HTMLElement, token: string): HTMLButtonElement {
  const el = host.querySelector<HTMLButtonElement>(`.cscp-swatch[data-token="${token}"]`);
  if (!el) throw new Error(`radio ${token} not rendered`);
  return el;
}

function pressKey(el: HTMLElement, key: string): void {
  el.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }));
}

describe('ChartSeriesColorPickerComponent', () => {
  it('renders a labelled radiogroup with one labelled radio per eligible token', () => {
    const { el } = setup();
    const group = el.querySelector<HTMLElement>('[role="radiogroup"]');
    expect(group).not.toBeNull();
    expect(group?.getAttribute('aria-label')).toBe('Chart series color');
    const radios = el.querySelectorAll<HTMLButtonElement>('[role="radio"]');
    expect(radios.length).toBe(10);
    expect(radioFor(el, 'series-blue').getAttribute('aria-label')).toBe('Blue');
    expect(radioFor(el, 'series-amber').getAttribute('aria-label')).toBe('Amber');
  });

  it('marks the selected token aria-checked with a non-color check glyph', () => {
    const { el } = setup('series-teal');
    const teal = radioFor(el, 'series-teal');
    expect(teal.getAttribute('aria-checked')).toBe('true');
    expect(teal.querySelector('.cscp-check')).not.toBeNull();
    expect(radioFor(el, 'series-blue').getAttribute('aria-checked')).toBe('false');
    expect(radioFor(el, 'series-blue').querySelector('.cscp-check')).toBeNull();
  });

  it('emits only token IDs on click', () => {
    const { fixture, el, tokenSelected } = setup();
    radioFor(el, 'series-orange').dispatchEvent(
      new MouseEvent('click', { bubbles: true, cancelable: true }),
    );
    fixture.detectChanges();
    expect(tokenSelected).toHaveBeenCalledWith('series-orange');
    expect(tokenSelected.mock.calls[0]?.[0]).toMatch(/^series-[a-z]+$/);
  });

  it('keeps a single tab stop — the selected radio, or the first when none', () => {
    const noneSel = setup();
    expect(radioFor(noneSel.el, 'series-blue').tabIndex).toBe(0);
    expect(radioFor(noneSel.el, 'series-red').tabIndex).toBe(-1);

    const tealSel = setup('series-teal');
    expect(radioFor(tealSel.el, 'series-teal').tabIndex).toBe(0);
    expect(radioFor(tealSel.el, 'series-blue').tabIndex).toBe(-1);
  });

  it('arrow keys move selection and focus together, wrapping around the ends', () => {
    let host: HTMLElement | null = null;
    let harness: Harness;
    try {
      harness = setup('series-blue');
      host = harness.el;
      document.body.appendChild(host);
      const first = radioFor(host, 'series-blue');
      first.focus();
      expect(document.activeElement).toBe(first);
      pressKey(first, 'ArrowRight');
      const sky = radioFor(host, 'series-sky');
      expect(document.activeElement).toBe(sky);
      // Selection follows focus: the emitted token updates aria-checked and
      // the roving tab stop along with the focus move.
      expect(harness.tokenSelected).toHaveBeenCalledWith('series-sky');
      pressKey(sky, 'ArrowLeft');
      expect(document.activeElement).toBe(first);
      expect(harness.tokenSelected).toHaveBeenCalledWith('series-blue');
      pressKey(first, 'ArrowUp'); // wraps to the last option
      const last = radioFor(host, 'series-violet');
      expect(document.activeElement).toBe(last);
      expect(harness.tokenSelected).toHaveBeenCalledWith('series-violet');
      pressKey(last, 'Home');
      expect(document.activeElement).toBe(first);
      expect(harness.tokenSelected).toHaveBeenCalledWith('series-blue');
      pressKey(first, 'End');
      expect(document.activeElement).toBe(last);
      expect(harness.tokenSelected).toHaveBeenCalledWith('series-violet');
    } finally {
      host?.remove();
    }
  });
});
