import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { DaemonDiagnosticReport } from '../../../api/daemon-diagnostics.types';
import { DaemonDiagnosticsPanelComponent } from './daemon-diagnostics-panel.component';

describe('DaemonDiagnosticsPanelComponent', () => {
  afterEach(() => TestBed.resetTestingModule());

  it('renders backend-authored copy without exposing raw check ids', () => {
    const fixture = TestBed.createComponent(DaemonDiagnosticsPanelComponent);
    fixture.componentRef.setInput('report', report());
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Control-plane lease is stale');
    expect(text).toContain('Renew the control-plane lease, then refresh diagnostics.');
    expect(text).not.toContain('daemon.control_plane_lease');
  });

  it('emits renew only for the backend-authored recovery action', () => {
    const fixture = TestBed.createComponent(DaemonDiagnosticsPanelComponent);
    const renew = vi.fn();
    fixture.componentInstance.renewLease.subscribe(renew);
    fixture.componentRef.setInput('report', report());
    fixture.detectChanges();

    const button = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('button'),
    ).find((candidate) => candidate.textContent?.includes('Renew control-plane lease')) as HTMLButtonElement | undefined;
    button?.click();

    expect(renew).toHaveBeenCalledTimes(1);
  });
});

function report(): DaemonDiagnosticReport {
  return {
    overall_status: 'warn',
    transport: 'CONNECTED',
    dominant_condition: 'lease_stale',
    headline: {
      title: 'Control-plane lease is stale',
      summary: 'The daemon is reachable, but its lease timestamp is older than allowed.',
      remediation: 'Renew the control-plane lease, then refresh diagnostics.',
    },
    checks: [
      {
        check_id: 'daemon.control_plane_lease',
        category: 'lease',
        status: 'warn',
        title: 'Control-plane lease is stale',
        summary: 'The daemon is reachable, but its lease timestamp is older than allowed.',
        technical_detail: null,
        remediation: 'Renew the control-plane lease, then refresh diagnostics.',
        scope: 'global',
        scope_ref: null,
        evidence: { facts: {}, redacted: false },
        action: {
          action_id: 'renew_lease',
          kind: 'recovery_mutation',
          label: 'Renew control-plane lease',
          endpoint: '/api/live-instances/daemon-health/renew-lease',
          confirm: true,
          deep_link: null,
        },
      },
      {
        check_id: 'daemon.code_freshness',
        category: 'code_freshness',
        status: 'pass',
        title: 'Live engine is running current code',
        summary: 'The running daemon commit matches the on-disk repo head.',
        technical_detail: null,
        remediation: null,
        scope: 'global',
        scope_ref: null,
        evidence: null,
        action: null,
      },
    ],
    per_instance: [],
    daemon_boot_id: 'boot-1',
    fetched_at_ms: 1_700_000_000_000,
  };
}
