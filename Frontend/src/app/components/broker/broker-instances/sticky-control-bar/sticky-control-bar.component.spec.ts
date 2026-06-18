import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import type {
  InstanceLastExit,
  InstanceProcessView,
  LiveInstanceStatus,
} from '../../../../api/live-instances.types';
import { StickyControlBarComponent } from './sticky-control-bar.component';

function makeStatus(
  overrides: Partial<LiveInstanceStatus> = {},
): LiveInstanceStatus {
  return {
    strategy_instance_id: 'spy_15m_breakout',
    process: { state: 'idle' } as InstanceProcessView,
    live_binding: null,
    evidence_binding: null,
    desired_state: null,
    readiness: null,
    latest_decision: null,
    decision_columns: [],
    broker: null,
    start_defaults: null,
    provenance: null,
    sizing: null,
    last_exit: null,
    symbol: null,
    action_plan: null,
    instrument_surface: null,
    fetched_at_ms: 0,
    ...overrides,
  };
}

function render(opts: {
  status: LiveInstanceStatus;
  isPaper?: boolean;
}): { el: HTMLElement; component: StickyControlBarComponent } {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(StickyControlBarComponent);
  fixture.componentRef.setInput('status', opts.status);
  fixture.componentRef.setInput('isPaper', opts.isPaper ?? true);
  fixture.detectChanges();
  return {
    el: fixture.nativeElement as HTMLElement,
    component: fixture.componentInstance,
  };
}

afterEach(() => TestBed.resetTestingModule());

describe('StickyControlBarComponent', () => {
  it('renders the PAPER pill when isPaper is true (User Story #3)', () => {
    const { el } = render({ status: makeStatus(), isPaper: true });

    const pill = el.querySelector<HTMLElement>('[data-testid="paper-pill"]');
    expect(pill).not.toBeNull();
    expect(pill?.textContent ?? '').toContain('PAPER');
    expect(pill?.getAttribute('data-verdict')).toBe('paper');
  });

  it('hides the PAPER pill on a live account', () => {
    const { el } = render({ status: makeStatus(), isPaper: false });

    expect(el.querySelector('[data-testid="paper-pill"]')).toBeNull();
  });

  it('renders the bot identity (User Story #2)', () => {
    const { el } = render({
      status: makeStatus({ strategy_instance_id: 'qqq_momentum' }),
    });

    expect(el.querySelector('[data-testid="bot-name"]')?.textContent?.trim()).toBe(
      'qqq_momentum',
    );
  });

  it.each([
    ['READY', 'STEADY'],
    ['DEGRADED', 'CONFIGURE'],
    ['BLOCKED', 'BLOCKED'],
    ['UNKNOWN', 'BLOCKED'],
  ] as const)('renders the %s readiness verdict as %s fleet-state pill', (verdict, label) => {
    const { el } = render({
      status: makeStatus({
        readiness: {
          kind: 'live_readiness',
          as_of_ms: 0,
          source: 'engine',
          verdict,
          summary: '',
          gates: [],
        },
      }),
    });

    const pill = el.querySelector<HTMLElement>('[data-testid="cockpit-fleet-state-pill"]');
    expect(pill?.textContent ?? '').toContain(label);
    expect(pill?.getAttribute('data-state')).toBe(label);
  });

  it('renders BLOCKED fleet-state when the engine has not emitted a readiness vector', () => {
    const { el } = render({ status: makeStatus() });

    const pill = el.querySelector<HTMLElement>('[data-testid="cockpit-fleet-state-pill"]');
    expect(pill?.getAttribute('data-state')).toBe('BLOCKED');
  });

  it('renders the poison chip when the last_exit carries a halt_trigger (User Story #9)', () => {
    const exit: InstanceLastExit = {
      run_id: 'run_abc',
      ended_at_ms: 0,
      exit_code: 1,
      exit_reason: 'fatal_halt',
      hydration_accepted: null,
      hydration_failure_reason: null,
      halt_trigger: 'outside_mutation',
      halt_at_ms: 0,
      halt_detail: null,
    };
    const { el } = render({ status: makeStatus({ last_exit: exit }) });

    expect(el.querySelector('[data-testid="poison-chip"]')).not.toBeNull();
  });

  it('does not render the poison chip on a clean last_exit', () => {
    const exit: InstanceLastExit = {
      run_id: 'run_abc',
      ended_at_ms: 0,
      exit_code: 0,
      exit_reason: 'normal',
      hydration_accepted: null,
      hydration_failure_reason: null,
      halt_trigger: null,
      halt_at_ms: null,
      halt_detail: null,
    };
    const { el } = render({ status: makeStatus({ last_exit: exit }) });

    expect(el.querySelector('[data-testid="poison-chip"]')).toBeNull();
  });

  it('emits jumpToControlsRequested when the operator clicks Jump to controls', () => {
    const { el, component } = render({ status: makeStatus() });
    let fired = 0;
    component.jumpToControlsRequested.subscribe(() => (fired += 1));

    el.querySelector<HTMLButtonElement>('[data-testid="jump-to-controls"]')?.click();

    expect(fired).toBe(1);
  });
});
