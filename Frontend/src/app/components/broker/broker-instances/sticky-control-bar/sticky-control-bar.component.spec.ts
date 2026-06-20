import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import type {
  InstanceLastExit,
  InstanceProcessView,
  LiveInstanceStatus,
  OperatorSurface,
} from '../../../../api/live-instances.types';
import { DEFAULT_OPERATOR_SURFACE } from '../../../../../testing/operator-surface-fixtures';
import { StickyControlBarComponent } from './sticky-control-bar.component';

function withSurface(overrides: Partial<OperatorSurface>): OperatorSurface {
  return { ...DEFAULT_OPERATOR_SURFACE, ...overrides };
}

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
    lineage: null,
    operator_surface: DEFAULT_OPERATOR_SURFACE,
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
  // PRD #607 / Slice 3 (#610): safety pill consumes the server-authored
  // operator_surface.broker.safety_verdict; the Frontend isPaper()
  // derivation no longer drives the banner pill.
  it.each([
    ['PAPER', 'PAPER-ONLY', 'paper'],
    ['LIVE', 'LIVE', 'ready'],
    ['DEGRADED', 'DEGRADED', 'degraded'],
    ['DISCONNECTED', 'DISCONNECTED', 'blocked'],
    ['UNKNOWN', 'UNKNOWN', 'unknown'],
  ] as const)(
    'renders SAFETY pill from operator_surface verdict %s -> %s',
    (verdict, label, tone) => {
      const { el } = render({
        status: makeStatus({
          operator_surface: withSurface({ broker: { safety_verdict: verdict } }),
        }),
        isPaper: false, // pill no longer depends on isPaper
      });
      const pill = el.querySelector<HTMLElement>('[data-testid="paper-pill"]');
      expect(pill).not.toBeNull();
      expect(pill?.textContent ?? '').toContain(label);
      expect(pill?.getAttribute('data-verdict')).toBe(tone);
    },
  );

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

  // PRD #607 / Slice 3 (#610) — banner keycaps.

  it('renders the strategy-key sub-line from start_defaults.strategy', () => {
    const { el } = render({
      status: makeStatus({
        start_defaults: {
          strategy: 'spy_ema_crossover',
          readonly: false,
          hydrate_policy: 'optional',
          max_orders_per_day: 50,
          ibkr_host: 'host',
        },
      }),
    });
    expect(
      el.querySelector('[data-testid="strategy-key-line"]')?.textContent?.trim(),
    ).toBe('spy_ema_crossover');
  });

  it('falls back to strategy_instance_id when start_defaults.strategy is empty', () => {
    const { el } = render({
      status: makeStatus({ strategy_instance_id: 'spy_qqq_paper' }),
    });
    expect(
      el.querySelector('[data-testid="strategy-key-line"]')?.textContent?.trim(),
    ).toBe('spy_qqq_paper');
  });

  it('renders Resume / Set intent: RUNNING based on effect discriminator', () => {
    const durable = render({
      status: makeStatus({
        operator_surface: withSurface({
          actions: {
            ...DEFAULT_OPERATOR_SURFACE.actions,
            resume: { enabled: true, effect: 'DURABLE_ONLY', disabled_reason_code: null },
          },
        }),
      }),
    });
    expect(
      durable.el
        .querySelector('[data-testid="banner-resume-keycap"]')
        ?.textContent?.toLowerCase(),
    ).toContain('set intent');

    const live = render({
      status: makeStatus({
        operator_surface: withSurface({
          actions: {
            ...DEFAULT_OPERATOR_SURFACE.actions,
            resume: { enabled: true, effect: 'LIVE_ACTUATION', disabled_reason_code: null },
          },
        }),
      }),
    });
    expect(
      live.el
        .querySelector('[data-testid="banner-resume-keycap"]')
        ?.textContent?.toLowerCase(),
    ).toContain('resume');
  });

  it('Resume keycap is enabled across every server snapshot (durable writes always succeed)', () => {
    const { el } = render({
      status: makeStatus({
        operator_surface: withSurface({
          actions: {
            ...DEFAULT_OPERATOR_SURFACE.actions,
            resume: { enabled: true, effect: 'DURABLE_ONLY', disabled_reason_code: null },
          },
        }),
      }),
    });
    const btn = el.querySelector<HTMLButtonElement>('[data-testid="banner-resume-keycap"]');
    expect(btn?.disabled).toBe(false);
  });

  it('Flatten keycap renders the reason-code tooltip when disabled', () => {
    const { el } = render({
      status: makeStatus({
        operator_surface: withSurface({
          actions: {
            ...DEFAULT_OPERATOR_SURFACE.actions,
            flatten_and_pause: {
              enabled: false,
              effect: 'LIVE_ACTUATION',
              disabled_reason_code: 'NO_LIVE_BINDING',
            },
          },
        }),
      }),
    });
    const btn = el.querySelector<HTMLButtonElement>('[data-testid="banner-flatten-keycap"]');
    expect(btn?.disabled).toBe(true);
    expect(btn?.title.length ?? 0).toBeGreaterThan(0);
  });

  it('keycaps disable locally on requestInFlight even when server enables them', () => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideZonelessChangeDetection()],
    });
    const fixture = TestBed.createComponent(StickyControlBarComponent);
    fixture.componentRef.setInput('status', makeStatus());
    fixture.componentRef.setInput('isPaper', true);
    fixture.componentRef.setInput('requestInFlight', true);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(
      el.querySelector<HTMLButtonElement>('[data-testid="banner-resume-keycap"]')?.disabled,
    ).toBe(true);
    expect(
      el.querySelector<HTMLButtonElement>('[data-testid="banner-pause-keycap"]')?.disabled,
    ).toBe(true);
    expect(
      el.querySelector<HTMLButtonElement>('[data-testid="banner-resume-keycap"]')?.title,
    ).toMatch(/in flight/i);
  });

  it.each([
    ['banner-resume-keycap', 'resumeRequested'],
    ['banner-pause-keycap', 'pauseRequested'],
    ['banner-flatten-keycap', 'flattenAndPauseRequested'],
  ] as const)('emits %s -> %s on click', (testid, output) => {
    const { el, component } = render({
      status: makeStatus({
        operator_surface: withSurface({
          actions: {
            resume: { enabled: true, effect: 'LIVE_ACTUATION', disabled_reason_code: null },
            pause: { enabled: true, effect: 'LIVE_ACTUATION', disabled_reason_code: null },
            flatten_and_pause: {
              enabled: true,
              effect: 'LIVE_ACTUATION',
              disabled_reason_code: null,
            },
            mark_poisoned: DEFAULT_OPERATOR_SURFACE.actions.mark_poisoned,
          },
        }),
      }),
    });
    let fired = 0;
    (component[output] as { subscribe: (fn: () => void) => unknown }).subscribe(
      () => (fired += 1),
    );
    el.querySelector<HTMLButtonElement>(`[data-testid="${testid}"]`)?.click();
    expect(fired).toBe(1);
  });
});
