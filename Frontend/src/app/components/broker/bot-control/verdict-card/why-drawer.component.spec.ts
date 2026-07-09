import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed, type ComponentFixture } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import type { OperatorGate } from '../../../../api/live-instances.types';
import { makeStatus } from '../bot-control-page.fixtures';
import { WhyDrawerComponent } from './why-drawer.component';

function renderDrawer(): ComponentFixture<WhyDrawerComponent> {
  TestBed.configureTestingModule({ providers: [provideZonelessChangeDetection()] });
  const fixture = TestBed.createComponent(WhyDrawerComponent);
  fixture.componentRef.setInput('open', true);
  return fixture;
}

const guidance = makeStatus().operator_surface.trader_guidance;

const failingGate: OperatorGate = {
  name: 'account.freeze',
  status: 'freeze',
  severity: 'hard',
  detail: 'Account is frozen.',
  gate_result: {
    gate_id: 'account.freeze',
    status: 'freeze',
    source: 'watchdog',
    operator_reason: 'The account is frozen after a timed-out flatten.',
    operator_next_step: null,
    evidence_at_ms: 0,
  },
  suggested_action: null,
  suggested_action_unavailable_reason: 'none',
};

describe('WhyDrawerComponent', () => {
  it('renders nothing while closed', () => {
    TestBed.configureTestingModule({ providers: [provideZonelessChangeDetection()] });
    const fixture = TestBed.createComponent(WhyDrawerComponent);
    fixture.componentRef.setInput('open', false);
    fixture.detectChanges();

    expect((fixture.nativeElement as HTMLElement).querySelector('[data-testid="why-drawer"]')).toBeNull();
  });

  it('renders the guidance claim, evidence, and a failing gate through the receipt pipe', () => {
    const fixture = renderDrawer();
    fixture.componentRef.setInput('guidance', guidance);
    fixture.componentRef.setInput('gates', [failingGate]);
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Broker state is not proven enough to submit.');
    // advanced_evidence label + value both piped through receiptLabel.
    expect(text).toContain('Broker Connection');
    expect(text).toContain('Disconnected');
    // failing gate name is piped; its operator_reason is rendered verbatim.
    expect(text).toContain('Account Freeze');
    expect(text).toContain('The account is frozen after a timed-out flatten.');
  });

  it('shows an honest empty state and never fabricates evidence', () => {
    const fixture = renderDrawer();
    fixture.componentRef.setInput('guidance', null);
    fixture.componentRef.setInput('gates', []);
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Not yet proven');
    expect(text).not.toContain('Unknown');
  });

  it('emits closed when the close button is clicked', () => {
    const fixture = renderDrawer();
    fixture.componentRef.setInput('guidance', guidance);
    fixture.detectChanges();

    const closed = vi.fn();
    fixture.componentInstance.closed.subscribe(closed);
    (fixture.nativeElement as HTMLElement)
      .querySelector<HTMLButtonElement>('.why-drawer__close')
      ?.click();

    expect(closed).toHaveBeenCalledTimes(1);
  });
});
