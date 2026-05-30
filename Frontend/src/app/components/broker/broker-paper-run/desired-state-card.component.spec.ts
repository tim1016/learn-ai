import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DesiredStateCardComponent } from './desired-state-card.component';
import type {
  DesiredState,
  DesiredStateAction,
  DesiredStatePathStatus,
} from '../../../api/live-runs.types';

function makeDesired(
  path_status: DesiredStatePathStatus,
  overrides: Partial<DesiredState> = {},
): DesiredState {
  return {
    state: path_status === 'ok' ? 'RUNNING' : null,
    updated_at_ms: path_status === 'ok' ? 1_700_000_000_000 : null,
    updated_by: path_status === 'ok' ? 'operator' : null,
    reason: null,
    version: path_status === 'ok' ? 3 : null,
    path_status,
    ...overrides,
  };
}

/** Host harness so we can drive the required signal input and capture output. */
@Component({
  imports: [DesiredStateCardComponent],
  template: `
    <app-desired-state-card
      [desired]="desired()"
      [strategyInstanceId]="'spy_ema_crossover_1min'"
      [runState]="'running'"
      [busy]="busy()"
      [writeError]="writeError()"
      (act)="onAct($event)"
    />
  `,
})
class HostComponent {
  readonly desired = signal<DesiredState>(makeDesired('ok'));
  readonly busy = signal(false);
  readonly writeError = signal<string | null>(null);
  readonly actions: DesiredStateAction[] = [];
  onAct(a: DesiredStateAction): void {
    this.actions.push(a);
  }
}

function setup(desired: DesiredState = makeDesired('ok')) {
  TestBed.configureTestingModule({ imports: [HostComponent] });
  const fixture = TestBed.createComponent(HostComponent);
  fixture.componentInstance.desired.set(desired);
  fixture.detectChanges();
  const el = fixture.nativeElement as HTMLElement;
  return { fixture, host: fixture.componentInstance, el };
}

function text(el: HTMLElement): string {
  return (el.textContent ?? '').replace(/\s+/g, ' ');
}

function buttonByText(el: HTMLElement, label: string): HTMLButtonElement | undefined {
  return Array.from(el.querySelectorAll('button')).find((b) =>
    (b.textContent ?? '').includes(label),
  );
}

function requireButton(el: HTMLElement, label: string): HTMLButtonElement {
  const btn = buttonByText(el, label);
  if (!btn) throw new Error(`Expected a button labelled "${label}"`);
  return btn;
}

function requireEl<T extends Element>(root: ParentNode, selector: string): T {
  const found = root.querySelector<T>(selector);
  if (!found) throw new Error(`Expected an element matching "${selector}"`);
  return found;
}

afterEach(() => {
  TestBed.resetTestingModule();
  vi.restoreAllMocks();
});

describe('DesiredStateCardComponent — read-only clarity (UI-2)', () => {
  it('renders the desired badge and sidecar provenance for an ok sidecar', () => {
    const { el } = setup(makeDesired('ok', { state: 'PAUSED' }));
    const t = text(el);
    expect(t).toContain('Desired State');
    expect(t).toContain('PAUSED');
    expect(t).toContain('desired-state sidecar');
  });

  it('shows an explicit "Unknown — no ledger binding" affordance and hides controls', () => {
    const { el } = setup(makeDesired('unknown_no_ledger_binding'));
    const t = text(el);
    expect(t).toContain('Unknown — no ledger binding');
    expect(buttonByText(el, 'Pause strategy')).toBeUndefined();
    expect(buttonByText(el, 'Resume strategy')).toBeUndefined();
  });

  it('treats an absent sidecar as RUNNING with an explicit default note', () => {
    const { el } = setup(makeDesired('absent'));
    const t = text(el);
    expect(t).toContain('RUNNING');
    expect(t).toContain('engine treats absent intent as RUNNING');
  });

  it('renders a corrupt sidecar error and blocks the controls', () => {
    const { el } = setup(makeDesired('corrupt'));
    const t = text(el);
    expect(t).toContain('Desired-state sidecar is corrupt');
    expect(buttonByText(el, 'Pause strategy')).toBeUndefined();
  });
});

describe('DesiredStateCardComponent — durable intent controls (UI-3)', () => {
  it('emits a pause action when Pause is clicked from RUNNING', () => {
    const { el, host } = setup(makeDesired('ok', { state: 'RUNNING' }));
    requireButton(el, 'Pause strategy').click();
    expect(host.actions).toEqual(['pause']);
  });

  it('disables Pause when already PAUSED and enables Resume', () => {
    const { el } = setup(makeDesired('ok', { state: 'PAUSED' }));
    expect(requireButton(el, 'Pause strategy').disabled).toBe(true);
    expect(requireButton(el, 'Resume strategy').disabled).toBe(false);
  });

  it('requires a confirm step before emitting stop', () => {
    const { fixture, el, host } = setup(makeDesired('ok', { state: 'RUNNING' }));
    requireButton(el, 'Stop strategy').click();
    fixture.detectChanges();
    expect(host.actions).toEqual([]);
    expect(text(el)).toContain('Stop durably?');

    requireButton(el, 'Confirm stop').click();
    expect(host.actions).toEqual(['stop']);
  });

  it('shows the write error returned by the parent', () => {
    const { fixture, el, host } = setup(makeDesired('ok', { state: 'RUNNING' }));
    host.writeError.set('sidecar is corrupt');
    fixture.detectChanges();
    expect(text(el)).toContain('sidecar is corrupt');
  });
});

describe('DesiredStateCardComponent — accessibility', () => {
  it('exposes the card via an aria-labelledby region with a real heading', () => {
    const { el } = setup(makeDesired('ok', { state: 'RUNNING' }));
    const section = requireEl(el, 'section[aria-labelledby]');
    const labelId = section.getAttribute('aria-labelledby') ?? '';
    const heading = el.querySelector(`#${labelId}`);
    expect(heading?.textContent).toContain('Desired State');
  });

  it('every control button is a real <button> with an accessible name', () => {
    const { el } = setup(makeDesired('ok', { state: 'RUNNING' }));
    const buttons = Array.from(el.querySelectorAll('.intent-controls button'));
    expect(buttons.length).toBeGreaterThan(0);
    for (const b of buttons) {
      expect(b.getAttribute('type')).toBe('button');
      expect((b.textContent ?? '').trim().length).toBeGreaterThan(0);
    }
  });

  it('groups the intent controls with an accessible group label', () => {
    const { el } = setup(makeDesired('ok', { state: 'RUNNING' }));
    const group = el.querySelector('.intent-controls');
    expect(group?.getAttribute('role')).toBe('group');
    expect(group?.getAttribute('aria-label')).toBeTruthy();
  });

  it('marks the corrupt error region as an alert for assistive tech', () => {
    const { el } = setup(makeDesired('corrupt'));
    const alert = el.querySelector('[role="alert"]');
    expect(alert?.textContent).toContain('corrupt');
  });
});
