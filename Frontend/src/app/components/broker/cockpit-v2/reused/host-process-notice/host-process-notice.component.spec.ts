import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';

import type {
  HostProcessState,
  OperatorSurfaceHostProcess,
} from '../../../../../api/live-instances.types';
import { HostProcessNoticeComponent } from './host-process-notice.component';

function host(overrides: Partial<OperatorSurfaceHostProcess> = {}): OperatorSurfaceHostProcess {
  return {
    state: 'IDLE',
    notice: 'Host runner is reachable but no subprocess is attached to this instance.',
    copyable_command: null,
    start_capability: {
      enabled: false,
      run_id: null,
      request: null,
      disabled_reason_code: 'START_SETTINGS_INCOMPLETE',
    },
    ...overrides,
  };
}

function render(opts: {
  hostProcess: OperatorSurfaceHostProcess;
  desiredIntent?: string | null;
}): HTMLElement {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(HostProcessNoticeComponent);
  fixture.componentRef.setInput('hostProcess', opts.hostProcess);
  fixture.componentRef.setInput('desiredIntent', opts.desiredIntent ?? null);
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

afterEach(() => TestBed.resetTestingModule());

describe('HostProcessNoticeComponent', () => {
  it('renders nothing when host_process.state is RUNNING', () => {
    const el = render({ hostProcess: host({ state: 'RUNNING', notice: null }) });
    expect(el.querySelector('[data-testid="host-process-notice"]')).toBeNull();
  });

  it.each([
    ['STOPPING', 'HOST PROCESS STOPPING'],
    ['EXITED', 'HOST PROCESS EXITED'],
    ['IDLE', 'HOST RUNNER IDLE'],
    ['WAITING_FOR_HOST', 'WAITING FOR HOST PROCESS'],
    ['UNREACHABLE', 'HOST RUNNER UNREACHABLE'],
  ] as const)('renders heading %s for state %s', (state, heading) => {
    const el = render({ hostProcess: host({ state, notice: 'x' }) });
    expect(el.textContent ?? '').toContain(heading);
  });

  it('renders the server-authored notice body verbatim', () => {
    const el = render({
      hostProcess: host({ notice: 'SERVER COPY DO NOT REWRITE' }),
    });
    expect(
      el.querySelector('[data-testid="host-process-notice-body"]')?.textContent?.trim(),
    ).toBe('SERVER COPY DO NOT REWRITE');
  });

  it('omits the notice row when the server sends notice: null', () => {
    const el = render({ hostProcess: host({ notice: null }) });
    expect(el.querySelector('[data-testid="host-process-notice-body"]')).toBeNull();
  });

  it('omits the copyable-command block when the server sends copyable_command: null', () => {
    const el = render({ hostProcess: host({ copyable_command: null }) });
    expect(el.querySelector('[data-testid="host-process-copyable-command"]')).toBeNull();
  });

  it('renders the copyable-command block verbatim when the server sends one', () => {
    const el = render({
      hostProcess: host({ copyable_command: 'python -m app.host_runner spy_ema_paper' }),
    });
    const block = el.querySelector('[data-testid="host-process-copyable-command"]');
    expect(block).not.toBeNull();
    expect(block?.textContent ?? '').toContain('python -m app.host_runner spy_ema_paper');
  });

  it('surfaces the desired-intent line when provided', () => {
    const el = render({
      hostProcess: host(),
      desiredIntent: 'RUNNING',
    });
    expect(
      el.querySelector('[data-testid="host-process-desired-intent"]')?.textContent ?? '',
    ).toContain('RUNNING');
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
