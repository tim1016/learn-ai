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
  it('with nothing selected, renders a labelled radiogroup with one labelled radio per eligible token and the first as the single tab stop', () => {
    const { el } = setup();
    const group = el.querySelector<HTMLElement>('[role="radiogroup"]');
    expect.soft(group, 'renders a radiogroup').not.toBeNull();
    expect.soft(group?.getAttribute('aria-label'), 'labels the radiogroup').toBe('Chart series color');
    const radios = el.querySelectorAll<HTMLButtonElement>('[role="radio"]');
    expect.soft(radios.length, 'renders one radio per eligible token').toBe(10);
    expect.soft(radioFor(el, 'series-blue').getAttribute('aria-label'), 'labels the Blue radio').toBe('Blue');
    expect.soft(radioFor(el, 'series-amber').getAttribute('aria-label'), 'labels the Amber radio').toBe('Amber');
    expect.soft(radioFor(el, 'series-blue').tabIndex, 'keeps a single tab stop (none selected): the first radio').toBe(0);
    expect.soft(radioFor(el, 'series-red').tabIndex, 'keeps a single tab stop (none selected): the other radios stay out of the tab order').toBe(-1);
  });

  it('with a token selected, marks it aria-checked with a non-color check glyph and makes it the single tab stop', () => {
    const { el } = setup('series-teal');
    const teal = radioFor(el, 'series-teal');
    expect.soft(teal.getAttribute('aria-checked'), 'marks the selected token aria-checked').toBe('true');
    expect.soft(teal.querySelector('.cscp-check'), 'marks the selected token with a non-color check glyph').not.toBeNull();
    expect.soft(radioFor(el, 'series-blue').getAttribute('aria-checked'), 'leaves an unselected token unchecked').toBe('false');
    expect.soft(radioFor(el, 'series-blue').querySelector('.cscp-check'), 'draws no check glyph on an unselected token').toBeNull();
    expect.soft(radioFor(el, 'series-teal').tabIndex, 'keeps a single tab stop (token selected): the selected radio').toBe(0);
    expect.soft(radioFor(el, 'series-blue').tabIndex, 'keeps a single tab stop (token selected): the other radios stay out of the tab order').toBe(-1);
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
