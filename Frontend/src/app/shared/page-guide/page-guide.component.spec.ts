import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { describe, expect, it } from 'vitest';
import { PageGuideComponent } from './page-guide.component';

function render(inputs: Record<string, unknown>): HTMLElement {
  TestBed.configureTestingModule({ providers: [provideRouter([])] });
  const fixture = TestBed.createComponent(PageGuideComponent);
  for (const [key, value] of Object.entries(inputs)) {
    fixture.componentRef.setInput(key, value);
  }
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

function find(el: HTMLElement, selector: string): Element {
  const node = el.querySelector(selector);
  if (node === null) throw new Error(`expected element matching ${selector}`);
  return node;
}

describe('PageGuideComponent', () => {
  it('renders pulls/why and the default summary label', () => {
    const el = render({
      pulls: 'Live IBKR option chain (SPY only).',
      why: 'Compose a multi-leg trade and inspect Greeks side-by-side.',
    });
    expect(find(el, '.page-guide-summary').textContent).toContain('How this page works');
    const meta = el.querySelectorAll('.page-guide-meta dd');
    expect(meta[0].textContent).toBe('Live IBKR option chain (SPY only).');
    expect(meta[1].textContent).toBe('Compose a multi-leg trade and inspect Greeks side-by-side.');
  });

  it('renders steps as an ordered list when provided', () => {
    const el = render({
      pulls: 'x',
      why: 'y',
      steps: ['Pick an expiry', 'Add legs', 'Inspect the payoff'],
    });
    const items = el.querySelectorAll('.page-guide-section ol li');
    expect(items.length).toBe(3);
    expect(items[0].textContent).toBe('Pick an expiry');
    expect(items[2].textContent).toBe('Inspect the payoff');
  });

  it('renders related links pointing at the right routes', () => {
    const el = render({
      pulls: 'x',
      why: 'y',
      related: [
        { label: 'Strategy Builder', route: '/strategy-builder' },
        { label: 'Pricing Lab', route: '/pricing-lab' },
      ],
    });
    const links = el.querySelectorAll('.page-guide-related a');
    expect(links.length).toBe(2);
    expect(links[0].textContent).toBe('Strategy Builder');
    expect(links[1].getAttribute('href')).toBe('/pricing-lab');
  });

  it('hides empty sections when steps and related are omitted', () => {
    const el = render({ pulls: 'x', why: 'y' });
    expect(el.querySelector('.page-guide-section')).toBeNull();
  });

  it('honors the open input', () => {
    const el = render({ pulls: 'x', why: 'y', open: true });
    const details = find(el, 'details') as HTMLDetailsElement;
    expect(details.open).toBe(true);
  });
});
