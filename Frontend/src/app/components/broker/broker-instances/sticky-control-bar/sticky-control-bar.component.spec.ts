import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import type {
  InstanceLastExit,
  InstanceProcessView,
  LiveInstanceStatus,
  ReadinessVector,
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
    fetched_at_ms: 0,
    ...overrides,
  };
}

function render(opts: {
  status: LiveInstanceStatus;
  isPaper?: boolean;
  cockpit?: boolean;
}): { el: HTMLElement; component: StickyControlBarComponent } {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(StickyControlBarComponent);
  fixture.componentRef.setInput('status', opts.status);
  fixture.componentRef.setInput('isPaper', opts.isPaper ?? true);
  fixture.componentRef.setInput('cockpit', opts.cockpit ?? false);
  fixture.detectChanges();
  return {
    el: fixture.nativeElement as HTMLElement,
    component: fixture.componentInstance,
  };
}

function makeReadyStatus(): LiveInstanceStatus {
  return {
    strategy_instance_id: 'spy_15m',
    process: { state: 'running' } as InstanceProcessView,
    live_binding: null,
    evidence_binding: null,
    desired_state: null,
    readiness: {
      kind: 'live_readiness',
      as_of_ms: 0,
      source: 'engine',
      verdict: 'READY',
      summary: '',
      gates: [],
    },
    latest_decision: null,
    decision_columns: [],
    broker: null,
    start_defaults: null,
    provenance: null,
    sizing: null,
    last_exit: null,
    symbol: null,
    fetched_at_ms: 0,
  } as unknown as LiveInstanceStatus;
}

afterEach(() => TestBed.resetTestingModule());

describe('StickyControlBarComponent', () => {
  it('renders the PAPER pill when isPaper is true (User Story #3)', () => {
    const { el } = render({ status: makeStatus(), isPaper: true });

    expect(el.querySelector('[data-testid="paper-pill"]')?.textContent?.trim()).toBe(
      'PAPER',
    );
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

  it('renders the RUNNING state pill in the running tone', () => {
    const { el } = render({
      status: makeStatus({ process: { state: 'running' } as InstanceProcessView }),
    });
    const pill = el.querySelector<HTMLElement>('[data-testid="state-pill"]');

    expect(pill?.textContent?.trim()).toBe('RUNNING');
    expect(pill?.classList.contains('running')).toBe(true);
  });

  it('renders STOPPED on idle process state', () => {
    const { el } = render({ status: makeStatus() });
    const pill = el.querySelector<HTMLElement>('[data-testid="state-pill"]');

    expect(pill?.textContent?.trim()).toBe('STOPPED');
  });

  it('renders the readiness pill verdict in the verdict tone', () => {
    const ready: ReadinessVector = {
      kind: 'live_readiness',
      as_of_ms: 0,
      source: 'engine',
      verdict: 'READY',
      summary: '',
      gates: [],
    };
    const { el } = render({ status: makeStatus({ readiness: ready }) });
    const pill = el.querySelector<HTMLElement>('[data-testid="readiness-pill"]');

    expect(pill?.textContent?.trim()).toBe('READY');
    expect(pill?.classList.contains('ready')).toBe(true);
  });

  it('renders BLOCKED readiness in the blocked tone', () => {
    const blocked: ReadinessVector = {
      kind: 'live_readiness',
      as_of_ms: 0,
      source: 'engine',
      verdict: 'BLOCKED',
      summary: '',
      gates: [],
    };
    const { el } = render({ status: makeStatus({ readiness: blocked }) });
    const pill = el.querySelector<HTMLElement>('[data-testid="readiness-pill"]');

    expect(pill?.classList.contains('blocked')).toBe(true);
  });

  it('renders NO READINESS when the engine has not emitted a vector', () => {
    const { el } = render({ status: makeStatus() });

    const pill = el.querySelector<HTMLElement>('[data-testid="readiness-pill"]');
    expect(pill?.textContent?.trim()).toBe('NO READINESS');
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

  describe('cockpit mode (broker-instances-v2 flag on)', () => {
    function statusWith(verdict: 'READY' | 'BLOCKED' | 'DEGRADED' | 'UNKNOWN' | undefined): LiveInstanceStatus {
      return {
        ...makeReadyStatus(),
        readiness: verdict
          ? {
              kind: 'live_readiness',
              as_of_ms: 0,
              source: 'engine',
              verdict,
              summary: '',
              gates: [],
            }
          : null,
      } as unknown as LiveInstanceStatus;
    }

    it.each([
      ['READY', 'STEADY'],
      ['DEGRADED', 'CONFIGURE'],
      ['BLOCKED', 'BLOCKED'],
      ['UNKNOWN', 'BLOCKED'],
    ] as const)('renders the %s readiness as %s fleet-state pill', (verdict, label) => {
      const { el } = render({ status: statusWith(verdict), cockpit: true });

      const pill = el.querySelector<HTMLElement>('[data-testid="cockpit-fleet-state-pill"]');
      expect(pill?.textContent ?? '').toContain(label);
      expect(pill?.getAttribute('data-state')).toBe(label);
    });

    it('hides the legacy state and readiness pills when cockpit is on', () => {
      const { el } = render({ status: makeReadyStatus(), cockpit: true });

      expect(el.querySelector('[data-testid="state-pill"]')).toBeNull();
      expect(el.querySelector('[data-testid="readiness-pill"]')).toBeNull();
    });

    it('renders the legacy pills when cockpit is off', () => {
      const { el } = render({ status: makeReadyStatus(), cockpit: false });

      expect(el.querySelector('[data-testid="state-pill"]')).not.toBeNull();
      expect(el.querySelector('[data-testid="readiness-pill"]')).not.toBeNull();
      expect(el.querySelector('[data-testid="cockpit-fleet-state-pill"]')).toBeNull();
    });
  });
});
