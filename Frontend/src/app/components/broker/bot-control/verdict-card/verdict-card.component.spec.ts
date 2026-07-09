import { ChangeDetectionStrategy, Component, input, provideZonelessChangeDetection } from '@angular/core';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed, type ComponentFixture } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import type {
  BotDailyLifecycleProjection,
  BotLifecycleCondition,
  LiveInstanceStatus,
} from '../../../../api/live-instances.types';
import type { RenderedAction } from '../lib/suggested-action-renderer';
import { makeStatus } from '../bot-control-page.fixtures';
import { makeDailyLifecycleFixture } from '../../../../testing/live-instance-status-fixtures';
import { ActivityTabComponent } from '../tabs/activity-tab.component';
import { VerdictCardComponent } from './verdict-card.component';

@Component({
  selector: 'app-activity-tab',
  template: '<div data-testid="activity-tab-stub"></div>',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
class ActivityTabStubComponent {
  readonly status = input.required<LiveInstanceStatus>();
}

function statusWith(
  lifecycle: Partial<BotDailyLifecycleProjection>,
  mutate?: (status: LiveInstanceStatus) => void,
): LiveInstanceStatus {
  const status = makeStatus();
  status.daily_lifecycle = makeDailyLifecycleFixture(lifecycle);
  mutate?.(status);
  return status;
}

function accountStaleCondition(): BotLifecycleCondition {
  return {
    scope: 'account',
    severity: 'warning',
    title: 'Account evidence stale',
    detail: 'Receipt acct-recon-DU1234567 expired before this triage snapshot.',
    owner_label: 'Account DU1234567',
    cure_action: 'reconcile_now',
    cure_label: 'Run account reconcile',
  };
}

function addRetiredTerminalBlocker(status: LiveInstanceStatus): void {
  status.operator_surface.blockers = [
    {
      id: 'retired',
      severity: 'blocking',
      disposition: 'terminal',
      headline: "Can't recover",
      detail: 'This bot has been retired. Remove it from the catalog or replace it.',
      primary_move: {
        label: 'Remove',
        action: { kind: 'remove' },
        target: 'delete',
      },
      secondary_moves: [
        {
          label: 'Replace',
          action: { kind: 'retire_replace' },
          target: 'retire_replace',
        },
      ],
      applies_to: 'run',
    },
  ];
}

function renderCard(
  status: LiveInstanceStatus,
  remediation: RenderedAction | null = null,
): ComponentFixture<VerdictCardComponent> {
  TestBed.configureTestingModule({
    providers: [
      provideZonelessChangeDetection(),
      provideHttpClient(),
      provideHttpClientTesting(),
    ],
  });
  TestBed.overrideComponent(VerdictCardComponent, {
    remove: { imports: [ActivityTabComponent] },
    add: { imports: [ActivityTabStubComponent] },
  });
  const fixture = TestBed.createComponent(VerdictCardComponent);
  fixture.componentRef.setInput('status', status);
  fixture.componentRef.setInput('renderedRemediation', remediation);
  fixture.detectChanges();
  return fixture;
}

describe('VerdictCardComponent', () => {
  it('renders the Ready state with the lifecycle start verb', () => {
    const fixture = renderCard(statusWith({ display_status: 'Ready' }));
    const el = fixture.nativeElement as HTMLElement;

    expect(el.querySelector('#verdict-state')?.textContent).toContain('Ready');
    expect(el.querySelector<HTMLButtonElement>('[data-testid="verdict-verb"]')?.textContent?.trim()).toBe(
      'Start',
    );
  });

  it('opens the scoped why drawer on demand', () => {
    const fixture = renderCard(statusWith({ display_status: 'Ready' }));
    const el = fixture.nativeElement as HTMLElement;

    expect(el.querySelector('[data-testid="why-drawer"]')).toBeNull();
    el.querySelector<HTMLButtonElement>('.vc-why')?.click();
    fixture.detectChanges();

    expect(el.querySelector('[data-testid="why-drawer"]')).not.toBeNull();
  });

  it('renders the remediation verb for a Sick bay bot and emits on click', () => {
    const invoke = vi.fn();
    const remediation: RenderedAction = { label: 'Reconcile now', variant: 'primary', invoke };
    const fixture = renderCard(
      statusWith({ display_status: 'Sick bay', primary_action: null }),
      remediation,
    );
    const el = fixture.nativeElement as HTMLElement;

    const remediationInvoked = vi.fn();
    fixture.componentInstance.remediationInvoked.subscribe(remediationInvoked);

    const verb = el.querySelector<HTMLButtonElement>('[data-testid="verdict-verb"]');
    expect(verb?.textContent?.trim()).toBe('Reconcile now');
    verb?.click();

    expect(remediationInvoked).toHaveBeenCalledTimes(1);
  });

  it('renders the Sick bay condition with its Account Monitor cure', () => {
    const fixture = renderCard(
      statusWith({
        display_status: 'Sick bay',
        primary_action: null,
        conditions: [accountStaleCondition()],
      }),
    );
    const el = fixture.nativeElement as HTMLElement;
    const accountMonitorRequested = vi.fn();
    fixture.componentInstance.accountMonitorRequested.subscribe(accountMonitorRequested);

    expect(el.textContent).toContain('Account evidence stale');
    expect(el.textContent).toContain(
      'Receipt acct-recon-DU1234567 expired before this triage snapshot.',
    );
    const button = Array.from(el.querySelectorAll<HTMLButtonElement>('.vc-condition button'))
      .find((candidate) => candidate.textContent?.includes('Run account reconcile'));
    expect(button).toBeDefined();

    button?.click();

    expect(accountMonitorRequested).toHaveBeenCalledTimes(1);
  });

  it('opens the why drawer when a self-runbook verb is clicked', () => {
    const fixture = renderCard(
      statusWith({ display_status: 'Sick bay', primary_action: null }, (status) => {
        status.operator_surface.trader_guidance.primary_remediation = {
          kind: 'open_runbook',
          slug: 'watchdog-halt',
        };
      }),
    );
    const el = fixture.nativeElement as HTMLElement;

    const verb = el.querySelector<HTMLButtonElement>('[data-testid="verdict-verb"]');
    expect(verb?.textContent?.trim()).toBe('View recovery details');
    expect(el.querySelector('[data-testid="why-drawer"]')).toBeNull();

    verb?.click();
    fixture.detectChanges();

    expect(el.querySelector('[data-testid="why-drawer"]')).not.toBeNull();
  });

  it('renders a retired terminal blocker with Replace and Remove moves', () => {
    // Default status has a reconcile remediation; terminal blockers own the card.
    const fixture = renderCard(
      statusWith(
        { display_status: 'Retired', phase: 'RETIRED', primary_action: null },
        addRetiredTerminalBlocker,
      ),
      { label: 'Reconcile now', variant: 'primary', invoke: vi.fn() },
    );
    const el = fixture.nativeElement as HTMLElement;
    const lifecycleAction = vi.fn();
    const removeRequested = vi.fn();
    fixture.componentInstance.lifecycleAction.subscribe(lifecycleAction);
    fixture.componentInstance.removeRequested.subscribe(removeRequested);

    expect(el.querySelector('.verdict-card')?.getAttribute('data-state')).toBe('Retired');
    expect(el.querySelector('#verdict-state')?.textContent).toContain("Can't recover");
    expect(el.querySelector('[data-testid="verdict-verb"]')).toBeNull();
    const buttons = Array.from(el.querySelectorAll<HTMLButtonElement>('.vc-terminal-action'));
    expect(buttons.map((button) => button.textContent?.trim())).toEqual(['Remove', 'Replace']);

    buttons[0]?.click();
    buttons[1]?.click();

    expect(removeRequested).toHaveBeenCalledTimes(1);
    expect(lifecycleAction).toHaveBeenCalledWith('retire_replace');
  });

  it('emits the ambient action id from the overflow menu', () => {
    const fixture = renderCard(statusWith({ display_status: 'Ready' }));
    const el = fixture.nativeElement as HTMLElement;

    const lifecycleAction = vi.fn();
    fixture.componentInstance.lifecycleAction.subscribe(lifecycleAction);

    el.querySelector<HTMLButtonElement>('.vc-overflow__trigger')?.click();
    fixture.detectChanges();

    const retire = Array.from(el.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')).find(
      (button) => button.textContent?.trim() === 'Retire & Replace',
    );
    retire?.click();

    expect(lifecycleAction).toHaveBeenCalledWith('retire_replace');
  });
});
