import { HttpErrorResponse } from '@angular/common/http';
import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type {
  GateResult,
  HostProcessStartCapability,
  HostProcessState,
  OperatorSurfaceHostProcess,
} from '../../../../../api/live-instances.types';
import type { HostRunnerActionResponse } from '../../../../../api/live-runs.types';
import { LiveRunsService } from '../../../../../services/live-runs.service';
import { HostProcessNoticeComponent } from './host-process-notice.component';

function gateResult(status: GateResult['status']): GateResult {
  return {
    gate_id: 'host_process.start',
    status,
    source: 'fixture',
    operator_reason: status === 'pass' ? 'GATE_PASSING' : 'START_DISABLED',
    operator_next_step: status === 'pass' ? null : 'Review the disabled reason.',
    evidence_at_ms: 0,
  };
}

const ENABLED_CAP: HostProcessStartCapability = {
  enabled: true,
  run_id: 'run-x',
  request: {
    readonly: true,
    hydrate_policy: 'require',
    strategy: 'spy_ema_crossover',
    max_orders_per_day: 50,
    ibkr_host: '127.0.0.1',
  },
  disabled_reason_code: null,
  gate_results: [gateResult('pass')],
};

const DISABLED_CAP_INCOMPLETE: HostProcessStartCapability = {
  enabled: false,
  run_id: null,
  request: null,
  disabled_reason_code: 'START_SETTINGS_INCOMPLETE',
  gate_results: [gateResult('block')],
};

function host(overrides: Partial<OperatorSurfaceHostProcess> = {}): OperatorSurfaceHostProcess {
  return {
    state: 'IDLE',
    notice: 'The host is reachable but this bot has no active process. Start it to resume trading.',
    copyable_command: null,
    start_capability: DISABLED_CAP_INCOMPLETE,
    ...overrides,
    last_exit_error_code: overrides.last_exit_error_code ?? null,
    last_exit_error_message: overrides.last_exit_error_message ?? null,
    last_exit_error_detail: overrides.last_exit_error_detail ?? {},
  };
}

interface RenderResult {
  el: HTMLElement;
  service: { startHostRunner: ReturnType<typeof vi.fn> };
}

function render(opts: {
  hostProcess: OperatorSurfaceHostProcess;
  desiredIntent?: string | null;
  priorRunClassification?: 'CLEAN' | 'HALT_TRIGGERED' | 'EXITED_WITH_ERROR' | 'UNKNOWN' | null;
  startResponse?: HostRunnerActionResponse | Promise<HostRunnerActionResponse>;
  startError?: unknown;
}): RenderResult {
  const startHostRunner = vi.fn();
  if (opts.startError !== undefined) {
    startHostRunner.mockRejectedValue(opts.startError);
  } else if (opts.startResponse !== undefined) {
    startHostRunner.mockResolvedValue(opts.startResponse);
  } else {
    startHostRunner.mockResolvedValue({
      accepted: true,
      process: { state: 'running', message: 'started' },
    } as unknown as HostRunnerActionResponse);
  }

  const serviceStub = { startHostRunner } as unknown as LiveRunsService;

  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      provideZonelessChangeDetection(),
      { provide: LiveRunsService, useValue: serviceStub },
    ],
  });
  const fixture = TestBed.createComponent(HostProcessNoticeComponent);
  fixture.componentRef.setInput('hostProcess', opts.hostProcess);
  fixture.componentRef.setInput('desiredIntent', opts.desiredIntent ?? null);
  fixture.componentRef.setInput('priorRunClassification', opts.priorRunClassification ?? null);
  fixture.detectChanges();
  return { el: fixture.nativeElement as HTMLElement, service: { startHostRunner } };
}

afterEach(() => TestBed.resetTestingModule());

describe('HostProcessNoticeComponent', () => {
  it('renders nothing when host_process.state is RUNNING', () => {
    const { el } = render({
      hostProcess: host({
        state: 'RUNNING',
        notice: null,
        start_capability: {
          enabled: false,
          run_id: null,
          request: null,
          disabled_reason_code: 'ALREADY_RUNNING',
          gate_results: [gateResult('block')],
        },
      }),
    });
    expect(el.querySelector('[data-testid="host-process-notice"]')).toBeNull();
  });

  it.each([
    ['STOPPING', 'HOST PROCESS STOPPING'],
    ['EXITED', 'HOST PROCESS EXITED'],
    ['IDLE', 'HOST RUNNER IDLE'],
    ['WAITING_FOR_HOST', 'WAITING FOR HOST PROCESS'],
    ['UNREACHABLE', 'HOST RUNNER UNREACHABLE'],
  ] as const)('renders heading %s for state %s', (state, heading) => {
    const { el } = render({ hostProcess: host({ state, notice: 'x' }) });
    expect(el.textContent ?? '').toContain(heading);
  });

  it('renders the server-authored notice body verbatim', () => {
    const { el } = render({
      hostProcess: host({ notice: 'SERVER COPY DO NOT REWRITE' }),
    });
    expect(
      el.querySelector('[data-testid="host-process-notice-body"]')?.textContent?.trim(),
    ).toBe('SERVER COPY DO NOT REWRITE');
  });

  it('omits the notice row when the server sends notice: null', () => {
    const { el } = render({ hostProcess: host({ notice: null }) });
    expect(el.querySelector('[data-testid="host-process-notice-body"]')).toBeNull();
  });

  it('omits the copyable-command block when the server sends copyable_command: null', () => {
    const { el } = render({ hostProcess: host({ copyable_command: null }) });
    expect(el.querySelector('[data-testid="host-process-copyable-command"]')).toBeNull();
  });

  it('renders the copyable-command block verbatim when the server sends one', () => {
    const { el } = render({
      hostProcess: host({ copyable_command: './start-live-daemon.sh --background' }),
    });
    const block = el.querySelector('[data-testid="host-process-copyable-command"]');
    expect(block).not.toBeNull();
    expect(block?.textContent ?? '').toContain('./start-live-daemon.sh --background');
  });

  it('surfaces the desired-intent line when provided', () => {
    const { el } = render({
      hostProcess: host(),
      desiredIntent: 'RUNNING',
    });
    expect(
      el.querySelector('[data-testid="host-process-desired-intent"]')?.textContent ?? '',
    ).toContain('RUNNING');
  });
});

describe('HostProcessNoticeComponent — Start bot process button', () => {
  it('renders the Start button enabled when start_capability.enabled is true', () => {
    const { el } = render({ hostProcess: host({ start_capability: ENABLED_CAP }) });
    const btn = el.querySelector<HTMLButtonElement>('[data-testid="host-process-start-button"]');
    expect(btn).not.toBeNull();
    expect(btn?.disabled).toBe(false);
    expect(btn?.textContent?.trim()).toBe('Start bot process');
    expect(
      el.querySelector('[data-testid="host-process-start-disabled-reason"]'),
    ).toBeNull();
  });

  it('disables the Start button and shows the trader-copy reason when disabled', () => {
    const { el } = render({ hostProcess: host({ start_capability: DISABLED_CAP_INCOMPLETE }) });
    const btn = el.querySelector<HTMLButtonElement>('[data-testid="host-process-start-button"]');
    expect(btn?.disabled).toBe(true);
    const reason = el.querySelector('[data-testid="host-process-start-disabled-reason"]');
    expect(reason?.textContent ?? '').toContain('saved start settings are incomplete');
  });

  it.each([
    ['ALREADY_RUNNING', 'already running'],
    ['STOPPING', 'shutting down'],
    ['HOST_SERVICE_OFFLINE', 'bot service is offline'],
    ['STOPPED_REQUIRES_REDEPLOY', 'permanently stopped'],
    ['START_SETTINGS_INCOMPLETE', 'saved start settings are incomplete'],
  ] as const)('maps disabled_reason_code %s to trader copy', (code, fragment) => {
    const { el } = render({
      hostProcess: host({
        start_capability: {
          enabled: false,
          run_id: null,
          request: null,
          disabled_reason_code: code,
          gate_results: [gateResult('block')],
        },
      }),
    });
    const reason = el.querySelector('[data-testid="host-process-start-disabled-reason"]');
    expect(reason?.textContent?.toLowerCase() ?? '').toContain(fragment);
  });

  it('invokes LiveRunsService.startHostRunner with the server-authored run_id and request on click', async () => {
    const { el, service } = render({ hostProcess: host({ start_capability: ENABLED_CAP }) });
    const btn = el.querySelector<HTMLButtonElement>('[data-testid="host-process-start-button"]');
    btn?.click();
    await Promise.resolve(); // let the in-flight async run
    expect(service.startHostRunner).toHaveBeenCalledTimes(1);
    expect(service.startHostRunner).toHaveBeenCalledWith('run-x', ENABLED_CAP.request);
  });

  it('shows an error message when the start call rejects', async () => {
    const err = new HttpErrorResponse({
      error: { detail: 'host daemon unreachable' },
      status: 503,
    });
    const { el } = render({
      hostProcess: host({ start_capability: ENABLED_CAP }),
      startError: err,
    });
    const btn = el.querySelector<HTMLButtonElement>('[data-testid="host-process-start-button"]');
    btn?.click();
    await new Promise<void>((r) => setTimeout(r, 0));
    const errMsg = el.querySelector('[data-testid="host-process-start-error"]');
    expect(errMsg?.textContent ?? '').toContain('host daemon unreachable');
  });

  it('does not call the service when start_capability is disabled', async () => {
    const { el, service } = render({
      hostProcess: host({ start_capability: DISABLED_CAP_INCOMPLETE }),
    });
    const btn = el.querySelector<HTMLButtonElement>('[data-testid="host-process-start-button"]');
    btn?.click();
    await Promise.resolve();
    expect(service.startHostRunner).not.toHaveBeenCalled();
  });
});

describe('HostProcessNoticeComponent — prior-run review-first advisory', () => {
  it('shows the HALT_TRIGGERED review advisory when prior_run.classification = HALT_TRIGGERED', () => {
    const { el } = render({
      hostProcess: host({ start_capability: ENABLED_CAP }),
      priorRunClassification: 'HALT_TRIGGERED',
    });
    const advisory = el.querySelector('[data-testid="host-process-review-first"]');
    expect(advisory).not.toBeNull();
    expect(advisory?.textContent ?? '').toContain('halted for safety');
    expect(advisory?.textContent ?? '').toContain('Warnings & interruptions');
  });

  it('shows the EXITED_WITH_ERROR review advisory when prior_run.classification = EXITED_WITH_ERROR', () => {
    const { el } = render({
      hostProcess: host({ start_capability: ENABLED_CAP }),
      priorRunClassification: 'EXITED_WITH_ERROR',
    });
    const advisory = el.querySelector('[data-testid="host-process-review-first"]');
    expect(advisory).not.toBeNull();
    expect(advisory?.textContent ?? '').toContain('ended with an error');
  });

  it.each(['CLEAN', 'UNKNOWN', null] as const)(
    'hides the review advisory when classification = %s',
    (cls) => {
      const { el } = render({
        hostProcess: host({ start_capability: ENABLED_CAP }),
        priorRunClassification: cls,
      });
      expect(el.querySelector('[data-testid="host-process-review-first"]')).toBeNull();
    },
  );

  it('keeps Start enabled when prior_run = HALT_TRIGGERED — the advisory does not gate Start', () => {
    // Design: "Guidance ranking does not disable Start. Any actual Start
    // prohibition must come from host_process.start_capability."
    const { el } = render({
      hostProcess: host({ start_capability: ENABLED_CAP }),
      priorRunClassification: 'HALT_TRIGGERED',
    });
    const btn = el.querySelector<HTMLButtonElement>('[data-testid="host-process-start-button"]');
    expect(btn?.disabled).toBe(false);
  });
});

it.each<HostProcessState>([
  'RUNNING',
  'STOPPING',
  'EXITED',
  'IDLE',
  'WAITING_FOR_HOST',
  'UNREACHABLE',
])(
  'host-process notice block accepts every documented state %s without crashing',
  (state) => {
    expect(() =>
      render({ hostProcess: host({ state, notice: state === 'RUNNING' ? null : 'x' }) }),
    ).not.toThrow();
  },
);
