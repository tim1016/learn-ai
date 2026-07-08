import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import type { LifecycleChartAction } from '../../../../api/live-instances.types';
import { OverviewActionsComponent } from './overview-actions.component';

const action = (
  overrides: Partial<LifecycleChartAction> & Pick<LifecycleChartAction, 'id' | 'label'>,
): LifecycleChartAction => ({
  enabled: true,
  reason_code: null,
  reason_headline: 'Available',
  reason_detail: 'Backend gates currently allow this action.',
  target_node_id: overrides.id,
  tone: 'secondary',
  ...overrides,
});

describe('OverviewActionsComponent', () => {
  it('renders all lifecycle actions as grouped icon-only toolbar buttons', () => {
    TestBed.configureTestingModule({
      providers: [provideZonelessChangeDetection()],
    });
    const fixture = TestBed.createComponent(OverviewActionsComponent);
    fixture.componentRef.setInput('actions', [
      action({ id: 'start_process', label: 'Start bot process', tone: 'primary' }),
      action({ id: 'resume', label: 'Resume' }),
      action({ id: 'pause', label: 'Pause' }),
      action({ id: 'flatten_and_pause', label: 'Flatten and pause', tone: 'danger' }),
      action({ id: 'stop', label: 'Stop', tone: 'danger' }),
      action({ id: 'redeploy', label: 'Redeploy' }),
      action({ id: 'mark_poisoned', label: 'Mark poisoned', tone: 'danger' }),
    ]);
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Run');
    expect(el.textContent).toContain('Recover');
    expect(el.textContent).toContain('Danger');
    expect(el.querySelector('[aria-label="Start bot process"]')?.textContent?.trim()).toBe('');
    expect(el.querySelector('[aria-label="Resume"]')).not.toBeNull();
    expect(el.querySelector('[aria-label="Pause"]')).not.toBeNull();
    expect(el.querySelector('[aria-label="Flatten and pause"]')).not.toBeNull();
    expect(el.querySelector('[aria-label="Stop"]')).not.toBeNull();
    expect(el.querySelector('[aria-label="Fresh run"]')).not.toBeNull();
    expect(el.querySelector('[aria-label="Mark poisoned"]')).not.toBeNull();
    for (const button of Array.from(el.querySelectorAll<HTMLButtonElement>('.chart-action'))) {
      expect(button.getAttribute('title')).toBeTruthy();
      expect(button.classList.contains('is-on')).toBe(true);
      expect(button.classList.contains('is-off')).toBe(false);
    }
    expect(el.querySelector('[aria-label="Pause"]')?.getAttribute('title'))
      .toContain('Pause On. Available');
  });

  it('shows backend action prose in the tooltip and off state when disabled', () => {
    TestBed.configureTestingModule({
      providers: [provideZonelessChangeDetection()],
    });
    const fixture = TestBed.createComponent(OverviewActionsComponent);
    fixture.componentRef.setInput('actions', [
      action({
        id: 'flatten_and_pause',
        label: 'Flatten and pause',
        enabled: false,
        reason_code: 'NO_LIVE_BINDING',
        reason_headline: 'No live binding',
        reason_detail: 'The lifecycle action contract says the runner is not bound.',
        tone: 'danger',
      }),
    ]);
    fixture.detectChanges();

    const button = (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>(
      '[aria-label="Flatten and pause"]',
    );
    expect(button?.getAttribute('aria-disabled')).toBe('true');
    expect(button?.classList.contains('is-off')).toBe(true);
    expect(button?.classList.contains('is-on')).toBe(false);
    expect(button?.getAttribute('title')).toContain('Flatten and pause Off');
    expect(button?.getAttribute('title')).toContain('No live binding');
    expect(button?.getAttribute('title')).toContain('runner is not bound');
    expect(button?.getAttribute('title')).not.toContain('NO_LIVE_BINDING');
  });

  it('keeps disabled actions selectable for lifecycle-node focus', () => {
    TestBed.configureTestingModule({
      providers: [provideZonelessChangeDetection()],
    });
    const fixture = TestBed.createComponent(OverviewActionsComponent);
    const disabledSelected = vi.spyOn(fixture.componentInstance.disabledActionSelected, 'emit');
    const invoked = vi.spyOn(fixture.componentInstance.actionInvoked, 'emit');
    fixture.componentRef.setInput('actions', [
      action({
        id: 'flatten_and_pause',
        label: 'Flatten and pause',
        enabled: false,
        target_node_id: 'recovery',
      }),
    ]);
    fixture.detectChanges();

    const button = (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>(
      '[aria-label="Flatten and pause"]',
    );
    button?.click();

    expect(disabledSelected).toHaveBeenCalledWith('recovery');
    expect(invoked).not.toHaveBeenCalled();
  });

  it('calls out when the backend marks Fresh run as the only available action', () => {
    TestBed.configureTestingModule({
      providers: [provideZonelessChangeDetection()],
    });
    const fixture = TestBed.createComponent(OverviewActionsComponent);
    fixture.componentRef.setInput('onlyFreshRunAvailable', true);
    fixture.componentRef.setInput('actions', [
      action({
        id: 'start_process',
        label: 'Start bot process',
        enabled: false,
        reason_headline: 'Stopped',
        reason_detail: 'Resume is required first.',
      }),
      action({
        id: 'resume',
        label: 'Resume',
        enabled: false,
        reason_headline: 'Broker safety unknown',
        reason_detail: 'Paper-only proof is missing.',
      }),
      action({
        id: 'pause',
        label: 'Pause',
        enabled: false,
        reason_headline: 'Already stopped',
        reason_detail: 'Pause is unavailable.',
      }),
      action({ id: 'redeploy', label: 'Redeploy' }),
    ]);
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('[data-testid="fresh-run-only-notice"]')?.textContent).toContain(
      'Only Fresh run is available',
    );
    expect(el.querySelector<HTMLButtonElement>('[aria-label="Fresh run"]')?.classList.contains('is-on'))
      .toBe(true);
    expect(el.querySelector<HTMLButtonElement>('[aria-label="Start bot process"]')?.classList.contains('is-off'))
      .toBe(true);
  });

  it('does not infer the Fresh-run-only notice from enabled actions', () => {
    TestBed.configureTestingModule({
      providers: [provideZonelessChangeDetection()],
    });
    const fixture = TestBed.createComponent(OverviewActionsComponent);
    fixture.componentRef.setInput('onlyFreshRunAvailable', false);
    fixture.componentRef.setInput('actions', [
      action({
        id: 'start_process',
        label: 'Start bot process',
        enabled: false,
        reason_headline: 'Stopped',
        reason_detail: 'Resume is required first.',
      }),
      action({ id: 'redeploy', label: 'Redeploy' }),
    ]);
    fixture.detectChanges();

    expect(
      (fixture.nativeElement as HTMLElement).querySelector('[data-testid="fresh-run-only-notice"]'),
    ).toBeNull();
  });
});
