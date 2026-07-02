import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import type { DecisionColumnDescriptor } from '../../../../../api/live-instances.types';
import { LatestSignalStripComponent } from './latest-signal-strip.component';

function makeCol(overrides: Partial<DecisionColumnDescriptor> = {}): DecisionColumnDescriptor {
  return {
    name: 'rsi_14',
    label: 'RSI(14)',
    type: 'number',
    format: 'decimal',
    semantic: 'momentum',
    ...overrides,
  };
}

function render(opts: {
  decisionColumns: DecisionColumnDescriptor[];
  latestDecision: Record<string, unknown> | null;
}): HTMLElement {
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(LatestSignalStripComponent);
  fixture.componentRef.setInput('decisionColumns', opts.decisionColumns);
  fixture.componentRef.setInput('latestDecision', opts.latestDecision);
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

afterEach(() => TestBed.resetTestingModule());

describe('LatestSignalStripComponent', () => {
  it('does not render when no columns and no signal are available', () => {
    const el = render({ decisionColumns: [], latestDecision: null });

    expect(el.querySelector('[data-testid="latest-signal-strip"]')).toBeNull();
  });

  it('renders the ENTER signal pill in the ok tone', () => {
    const el = render({
      decisionColumns: [],
      latestDecision: { signal: 'ENTER' },
    });

    const pill = el.querySelector<HTMLElement>('[data-testid="latest-signal-pill"]');
    expect(pill).not.toBeNull();
    expect(pill?.textContent ?? '').toContain('ENTER');
    expect(pill?.classList.contains('ok')).toBe(true);
  });

  it('renders the EXIT signal pill in the warn tone', () => {
    const el = render({
      decisionColumns: [],
      latestDecision: { signal: 'EXIT' },
    });

    const pill = el.querySelector<HTMLElement>('[data-testid="latest-signal-pill"]');
    expect(pill?.classList.contains('warn')).toBe(true);
  });

  it('renders an empty-state pill when columns are present but no signal yet', () => {
    const el = render({
      decisionColumns: [makeCol()],
      latestDecision: null,
    });

    expect(
      el.querySelector('[data-testid="latest-signal-pill-empty"]'),
    ).not.toBeNull();
    expect((el.textContent ?? '').toLowerCase()).toContain('no decision yet');
  });

  it('renders descriptor-backed cells with their declared label, in declared order, and skips the signal column', () => {
    const el = render({
      decisionColumns: [
        makeCol({ name: 'signal', label: 'Signal', format: 'string', semantic: undefined }),
        makeCol({ name: 'rsi_14', label: 'RSI(14)' }),
        makeCol({ name: 'ema_50', label: 'EMA(50)' }),
      ],
      latestDecision: { signal: 'HOLD', rsi_14: 42.5, ema_50: 100.123 },
    });

    const cells = el.querySelectorAll<HTMLElement>('.cell .cell-label');
    expect(Array.from(cells).map((c) => c.textContent)).toEqual(['RSI(14)', 'EMA(50)']);

    const text = el.textContent ?? '';
    expect(text).toContain('42.50');
    expect(text).toContain('100.12');
  });

  it('renders an em-dash for missing values in present columns', () => {
    const el = render({
      decisionColumns: [makeCol({ name: 'rsi_14', label: 'RSI(14)' })],
      latestDecision: { signal: 'HOLD' },
    });

    const text = el.textContent ?? '';
    expect(text).toContain('—');
  });

  it('uses the descriptor.semantic as the cell tooltip when present', () => {
    const el = render({
      decisionColumns: [makeCol({ semantic: 'momentum' })],
      latestDecision: { rsi_14: 50 },
    });

    const cell = el.querySelector<HTMLElement>('.cell');
    expect(cell?.getAttribute('title')).toBe('momentum');
  });
});
