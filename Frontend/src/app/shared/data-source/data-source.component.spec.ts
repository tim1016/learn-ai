import { TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';
import { DataSourceComponent } from './data-source.component';

function render(inputs: Record<string, unknown>): HTMLElement {
  const fixture = TestBed.createComponent(DataSourceComponent);
  fixture.componentRef.setInput('origin', inputs['origin']);
  if ('method' in inputs) fixture.componentRef.setInput('method', inputs['method']);
  if ('freshness' in inputs) fixture.componentRef.setInput('freshness', inputs['freshness']);
  if ('delayedFallback' in inputs) {
    fixture.componentRef.setInput('delayedFallback', inputs['delayedFallback']);
  }
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

function textOf(el: HTMLElement, selector: string): string {
  const node = el.querySelector(selector);
  if (node === null) throw new Error(`expected element matching ${selector}`);
  return node.textContent ?? '';
}

function find(el: HTMLElement, selector: string): Element {
  const node = el.querySelector(selector);
  if (node === null) throw new Error(`expected element matching ${selector}`);
  return node;
}

describe('DataSourceComponent', () => {
  it('renders origin + method + freshness joined with mid-dot separators', () => {
    const el = render({
      origin: 'IBKR',
      method: 'OPRA top-of-book, SMART-routed',
      freshness: 'live · last tick 14:23:01 ET',
    });
    expect(textOf(el, '.data-source-origin')).toBe('IBKR');
    const text = textOf(el, '.data-source-text');
    expect(text).toContain('OPRA top-of-book, SMART-routed');
    expect(text).toContain('live · last tick 14:23:01 ET');
  });

  it('omits missing pieces gracefully', () => {
    const el = render({ origin: 'Polygon', freshness: 'cached · 2026-05-03' });
    expect(textOf(el, '.data-source-text')).toBe('cached · 2026-05-03');
  });

  it('flips to amber 15-min delayed copy when delayedFallback=true', () => {
    const el = render({
      origin: 'IBKR',
      method: 'OPRA top-of-book',
      freshness: 'live',
      delayedFallback: true,
    });
    const caption = find(el, '.data-source');
    expect(caption.classList.contains('data-source--delayed')).toBe(true);
    expect(caption.getAttribute('role')).toBe('status');
    expect(textOf(el, '.data-source-text')).toBe('15-min delayed (no OPRA subscription)');
  });

  it('does not announce status when not in the delayed fallback', () => {
    const el = render({ origin: 'IBKR', method: 'top-of-book', freshness: 'live' });
    const caption = find(el, '.data-source');
    expect(caption.getAttribute('role')).toBeNull();
    expect(caption.classList.contains('data-source--delayed')).toBe(false);
  });
});
