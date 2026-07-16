import { TestBed } from '@angular/core/testing';
import { provideZonelessChangeDetection } from '@angular/core';
import { describe, expect, it } from 'vitest';

import { CohortLaunchDialogComponent } from './cohort-launch-dialog.component';

describe('CohortLaunchDialogComponent', () => {
  it('lists every hard blocker and disables authorization', async () => {
    await TestBed.configureTestingModule({
      imports: [CohortLaunchDialogComponent],
      providers: [provideZonelessChangeDetection()],
    }).compileComponents();
    const fixture = TestBed.createComponent(CohortLaunchDialogComponent);
    fixture.componentRef.setInput('open', true);
    fixture.componentRef.setInput('loading', false);
    fixture.componentRef.setInput('candidates', [{
      candidate: { strategyInstanceId: 'spy-a', name: 'SPY validation', strategyKey: 'spy_ema' },
      blockers: [{
        condition: { id: 'fleet_contaminated', severity: 'blocking', scope: 'fleet', evidence: {} },
        host: 'deploy_preflight',
        anchor: { kind: 'surface', subject_key: null },
        audience: 'operator',
        disposition: 'fix_elsewhere',
        headline: 'Fleet contamination blocks starts',
        detail: 'Clear fleet state.',
        primary_move: null,
        secondary_moves: [],
        applies_to: 'both',
      }],
      error: null,
    }]);
    fixture.detectChanges();

    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).toContain('Fleet contamination blocks starts');
    expect(root.querySelector<HTMLButtonElement>('button[disabled]')?.textContent).toContain('Authorize 1 bots');
    expect(root.querySelector('[role="alertdialog"]')?.getAttribute('aria-modal')).toBe('true');
  });

  it('disables authorization when a member preflight is unavailable', async () => {
    await TestBed.configureTestingModule({
      imports: [CohortLaunchDialogComponent],
      providers: [provideZonelessChangeDetection()],
    }).compileComponents();
    const fixture = TestBed.createComponent(CohortLaunchDialogComponent);
    fixture.componentRef.setInput('open', true);
    fixture.componentRef.setInput('loading', false);
    fixture.componentRef.setInput('candidates', [{
      candidate: { strategyInstanceId: 'spy-a', name: 'SPY validation', strategyKey: 'spy_ema' },
      blockers: [],
      error: 'The deploy preflight is unavailable.',
    }]);
    fixture.detectChanges();

    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).toContain('The deploy preflight is unavailable.');
    expect(root.querySelector<HTMLButtonElement>('button[disabled]')?.textContent).toContain('Authorize 1 bots');
  });
});
