import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import type { BotLifecycleAction } from '../../../../api/live-instances.types';
import { OverviewActionsComponent } from './overview-actions.component';

const action = (
  overrides: Partial<BotLifecycleAction> & Pick<BotLifecycleAction, 'id' | 'label'>,
): BotLifecycleAction => ({
  enabled: true,
  reason: null,
  offer_id: null,
  expires_at_ms: null,
  ...overrides,
});

const findActionButton = (element: HTMLElement, label: string): HTMLButtonElement | null =>
  element.querySelector<HTMLButtonElement>(`.chart-action[aria-label="${label}"]`);

const actionTitle = (button: HTMLButtonElement | null): string => {
  expect(button).not.toBeNull();
  if (!button) throw new Error('Expected lifecycle action button.');
  return button.closest<HTMLElement>('.chart-action-shell')?.getAttribute('title')
    ?? button.getAttribute('title')
    ?? '';
};

describe('OverviewActionsComponent', () => {
  it('renders daily lifecycle actions as grouped labeled toolbar buttons', () => {
    TestBed.configureTestingModule({
      providers: [provideZonelessChangeDetection()],
    });
    const fixture = TestBed.createComponent(OverviewActionsComponent);
    fixture.componentRef.setInput('actions', [
      action({ id: 'confirm_start', label: 'Start' }),
      action({ id: 'end_day_now', label: 'End day now' }),
      action({ id: 'take_off_roster', label: 'Take off roster' }),
      action({ id: 'retire_replace', label: 'Retire & Replace' }),
    ]);
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Duty');
    expect(el.textContent).toContain('Roster');
    expect(el.textContent).toContain('Machinery');
    expect(findActionButton(el, 'Start')?.textContent?.trim()).toBe('Start');
    expect(findActionButton(el, 'End day now')).not.toBeNull();
    expect(findActionButton(el, 'Take off roster')).not.toBeNull();
    expect(findActionButton(el, 'Retire & Replace')).not.toBeNull();
    expect(el.textContent).not.toContain('Resume');
    expect(el.textContent).not.toContain('Pause');
    expect(el.textContent).not.toContain('Fresh run only');
    for (const button of Array.from(el.querySelectorAll<HTMLButtonElement>('.chart-action'))) {
      expect(actionTitle(button)).toBeTruthy();
      expect(button.disabled).toBe(false);
      expect(button.classList.contains('is-on')).toBe(true);
      expect(button.classList.contains('is-off')).toBe(false);
    }
    expect(actionTitle(findActionButton(el, 'End day now'))).toContain('End day now On. Available');
  });

  it('shows backend action prose in the tooltip and off state when disabled', () => {
    TestBed.configureTestingModule({
      providers: [provideZonelessChangeDetection()],
    });
    const fixture = TestBed.createComponent(OverviewActionsComponent);
    fixture.componentRef.setInput('actions', [
      action({
        id: 'retire_replace',
        label: 'Retire & Replace',
        enabled: false,
        reason: 'This bot is still on duty.',
      }),
    ]);
    fixture.detectChanges();

    const button = findActionButton(fixture.nativeElement as HTMLElement, 'Retire & Replace');
    expect(button?.getAttribute('aria-disabled')).toBe('true');
    expect(button?.disabled).toBe(true);
    expect(button?.classList.contains('is-off')).toBe(true);
    expect(button?.classList.contains('is-on')).toBe(false);
    const title = actionTitle(button);
    expect(title).toContain('Retire & Replace Off');
    expect(title).toContain('This bot is still on duty.');
  });

  it('semantically disables unavailable actions and suppresses dispatch', () => {
    TestBed.configureTestingModule({
      providers: [provideZonelessChangeDetection()],
    });
    const fixture = TestBed.createComponent(OverviewActionsComponent);
    const invoked = vi.spyOn(fixture.componentInstance.actionInvoked, 'emit');
    fixture.componentRef.setInput('actions', [
      action({
        id: 'end_day_now',
        label: 'End day now',
        enabled: false,
        reason: 'The bot is already off duty.',
      }),
    ]);
    fixture.detectChanges();

    const button = (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>(
      '[aria-label="End day now"]',
    );
    expect(button?.disabled).toBe(true);
    button?.click();

    expect(invoked).not.toHaveBeenCalled();
  });
});
