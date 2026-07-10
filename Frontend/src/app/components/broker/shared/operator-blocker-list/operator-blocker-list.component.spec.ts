import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import type { OperatorBlocker } from '../../../../api/operator-blocker.types';
import { OperatorBlockerListComponent } from './operator-blocker-list.component';

const blocker: OperatorBlocker = {
  condition: {
    id: 'broker_disconnected',
    severity: 'blocking',
    scope: 'broker',
    evidence: {},
  },
  host: 'bot_cockpit',
  disposition: 'fix_elsewhere',
  headline: 'Broker disconnected',
  detail: 'Connect the IBKR session before starting this bot.',
  primary_move: {
    label: 'Connect the broker',
    action: { kind: 'navigate', route: '/broker', fragment: null },
    target: null,
  },
  secondary_moves: [],
  applies_to: 'both',
};

describe('OperatorBlockerListComponent', () => {
  it('renders backend-authored blocker copy and emits selected moves', () => {
    TestBed.configureTestingModule({ providers: [provideZonelessChangeDetection()] });
    const fixture = TestBed.createComponent(OperatorBlockerListComponent);
    fixture.componentRef.setInput('blockers', [blocker]);
    const selected = vi.fn();
    fixture.componentInstance.moveSelected.subscribe(selected);
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Broker disconnected');
    expect(el.textContent).toContain('Broker · Fix Elsewhere');

    el.querySelector<HTMLButtonElement>('.operator-blocker-list__move')?.click();

    expect(selected).toHaveBeenCalledWith({
      blocker,
      move: blocker.primary_move,
    });
  });

  it('renders warning rows without treating them as terminal blockers', () => {
    TestBed.configureTestingModule({ providers: [provideZonelessChangeDetection()] });
    const fixture = TestBed.createComponent(OperatorBlockerListComponent);
    fixture.componentRef.setInput('blockers', [
      {
        ...blocker,
        condition: { ...blocker.condition, severity: 'warning' },
      },
    ]);
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Broker disconnected');
    expect(el.querySelector('.operator-blocker-list__item.severity-warning')).toBeTruthy();
  });
});
