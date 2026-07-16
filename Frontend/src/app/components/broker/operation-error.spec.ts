import { HttpErrorResponse } from '@angular/common/http';
import { describe, expect, it } from 'vitest';
import {
  describeOperationError,
  readOutcomeUnknownBody,
  readPreconditionBody,
  toOperationError,
} from './operation-error';

describe('describeOperationError', () => {
  it('maps a 409 deploy to a precondition with deploy-specific remediation', () => {
    const e = describeOperationError('deploy', 409, 'Working tree is dirty; commit or stash.');
    expect(e.category).toBe('precondition');
    expect(e.title).toContain('Deploy');
    expect(e.detail).toBe('Working tree is dirty; commit or stash.');
    expect(e.remediation.toLowerCase()).toContain('commit');
  });

  it('maps a 503 to infra with start-specific remediation', () => {
    const e = describeOperationError('start', 503, 'host daemon unreachable');
    expect(e.category).toBe('infra');
    expect(e.remediation.toLowerCase()).toContain('live engine');
  });

  it('maps a 409 command to a no-binding precondition', () => {
    const e = describeOperationError('flatten', 409, 'no live run bound to this instance');
    expect(e.category).toBe('precondition');
    expect(e.remediation.toLowerCase()).toContain('start the instance');
  });

  it('treats a null status as an infra (transport) failure', () => {
    const e = describeOperationError('stop', null, 'connection refused');
    expect(e.category).toBe('infra');
  });

  it('falls back to a generic remediation for an unmapped status', () => {
    const e = describeOperationError('pause', 418, 'teapot');
    expect(e.category).toBe('unknown');
    expect(e.remediation).toBeTruthy();
  });

  it('never derives remediation from the detail string', () => {
    // Two different detail strings on the same (operation, status) yield the
    // SAME remediation — proving the wording is not parsed.
    const a = describeOperationError('deploy', 409, 'dirty tree at PythonDataService');
    const b = describeOperationError('deploy', 409, 'totally different wording here');
    expect(a.remediation).toBe(b.remediation);
  });
});

describe('toOperationError', () => {
  it('extracts status and FastAPI {detail} from an HttpErrorResponse', () => {
    const err = new HttpErrorResponse({ status: 409, error: { detail: 'dirty tree' } });
    const e = toOperationError('deploy', err);
    expect(e.status).toBe(409);
    expect(e.detail).toBe('dirty tree');
    expect(e.category).toBe('precondition');
  });

  it('treats status 0 (connection refused) as a transport failure (null status, infra)', () => {
    const err = new HttpErrorResponse({ status: 0, error: null });
    const e = toOperationError('start', err);
    expect(e.status).toBeNull();
    expect(e.category).toBe('infra');
  });

  it('handles a plain string error body', () => {
    const err = new HttpErrorResponse({ status: 400, error: 'bad input' });
    const e = toOperationError('deploy', err);
    expect(e.detail).toBe('bad input');
  });

  it('handles a non-HTTP Error', () => {
    const e = toOperationError('stop', new Error('boom'));
    expect(e.detail).toBe('boom');
    expect(e.status).toBeNull();
  });

  // ── PRD #619-C5 — ambiguous-outcome 409 ─────────────────────────────────

  it('surfaces the structured OUTCOME_UNKNOWN body as the outcome-unknown category', () => {
    const err = new HttpErrorResponse({
      status: 409,
      error: {
        detail: {
          outcome: 'UNKNOWN',
          reason_code: 'OUTCOME_UNKNOWN',
          error_category: 'read_timeout',
          detail: 'response lost',
          endpoint: 'start_run',
          occurred_at_ms: 1_700_000_000_000,
          runbook_hint: 'Refresh the cockpit to read live state before retrying.',
          mutation_attempt_id: 'mutation-1',
          mutation_dispatch_state: 'OUTCOME_UNKNOWN',
        },
      },
    });

    const e = toOperationError('start', err);

    expect(e.status).toBe(409);
    expect(e.category).toBe('outcome-unknown');
    expect(e.title).toContain('outcome unknown');
    expect(e.detail).toBe('response lost');
    expect(e.remediation).toBe('Refresh the cockpit to read live state before retrying.');
    expect(e.mutation_attempt_id).toBe('mutation-1');
    expect(e.mutation_dispatch_state).toBe('OUTCOME_UNKNOWN');
  });

  it('falls back to a synthesised detail when the OUTCOME_UNKNOWN body omits detail', () => {
    const err = new HttpErrorResponse({
      status: 409,
      error: {
        detail: {
          outcome: 'UNKNOWN',
          reason_code: 'OUTCOME_UNKNOWN',
          error_category: 'write_timeout',
          detail: null,
          endpoint: 'deploy',
          occurred_at_ms: 1_700_000_000_000,
          runbook_hint: 'Refresh before retrying.',
        },
      },
    });

    const e = toOperationError('deploy', err);

    expect(e.detail).toContain('write_timeout');
    expect(e.remediation).toBe('Refresh before retrying.');
  });

  it('accepts renew-daemon-lease outcome unknown responses', () => {
    const err = new HttpErrorResponse({
      status: 409,
      error: {
        detail: {
          outcome: 'UNKNOWN',
          reason_code: 'OUTCOME_UNKNOWN',
          error_category: 'read_timeout',
          detail: 'lease response lost',
          endpoint: 'renew_daemon_lease',
          occurred_at_ms: 1_700_000_000_000,
          runbook_hint: 'Refresh Bot Control before retrying.',
        },
      },
    });

    const e = toOperationError('renew-lease', err);

    expect(e.category).toBe('outcome-unknown');
    expect(e.detail).toBe('lease response lost');
    expect(e.remediation).toBe('Refresh Bot Control before retrying.');
  });

  it('rejects outcome-unknown bodies with endpoints outside the closed contract', () => {
    const parsed = readOutcomeUnknownBody({
      detail: {
        outcome: 'UNKNOWN',
        reason_code: 'OUTCOME_UNKNOWN',
        error_category: 'read_timeout',
        detail: 'cancel response lost',
        endpoint: 'cancel_order',
        occurred_at_ms: 1_700_000_000_000,
        runbook_hint: 'Refresh before retrying.',
      },
    });

    expect(parsed).toBeNull();
  });

  it('parses structured deterministic precondition bodies', () => {
    const parsed = readPreconditionBody({
      detail: {
        reason_code: 'STOPPED_REQUIRES_RESUME',
        message: 'DIagVal6 is durably STOPPED.',
        remediation: 'Use Resume to clear the stop latch.',
        gate_id: 'desired_state.start',
      },
    });

    expect(parsed).toEqual({
      reason_code: 'STOPPED_REQUIRES_RESUME',
      message: 'DIagVal6 is durably STOPPED.',
      remediation: 'Use Resume to clear the stop latch.',
      gate_id: 'desired_state.start',
    });
  });

  it('uses server-authored remediation for structured precondition bodies', () => {
    const err = new HttpErrorResponse({
      status: 409,
      error: {
        detail: {
          reason_code: 'STOPPED_REQUIRES_RESUME',
          message: 'DIagVal6 is durably STOPPED. Resume the bot to clear the stop latch.',
          remediation: 'Use Resume to set desired_state=RUNNING, then start the bot.',
          gate_id: 'desired_state.start',
        },
      },
    });

    const e = toOperationError('deploy', err);

    expect(e.category).toBe('precondition');
    expect(e.detail).toBe('DIagVal6 is durably STOPPED. Resume the bot to clear the stop latch.');
    expect(e.remediation).toBe('Use Resume to set desired_state=RUNNING, then start the bot.');
    expect(e.remediation).not.toContain('working tree is dirty');
    expect(e.reason_code).toBe('STOPPED_REQUIRES_RESUME');
    expect(e.gate_id).toBe('desired_state.start');
  });

  it('does not replace a typed precondition without remediation with legacy deploy advice', () => {
    const err = new HttpErrorResponse({
      status: 409,
      error: {
        detail: {
          reason_code: 'DEPLOY_PREFLIGHT_BLOCKED',
          message: 'A server launch check has not passed.',
          gate_id: 'broker.connection',
        },
      },
    });

    const e = toOperationError('deploy', err);

    expect(e.remediation).toBe('A precondition is not met. Resolve the conflict and retry.');
    expect(e.remediation).not.toContain('working tree is dirty');
    expect(e.reason_code).toBe('DEPLOY_PREFLIGHT_BLOCKED');
    expect(e.gate_id).toBe('broker.connection');
  });

  it('preserves typed deployment blockers and their recovery moves from a 409 launch race', () => {
    const err = new HttpErrorResponse({
      status: 409,
      error: {
        detail: {
          reason_code: 'DEPLOY_PREFLIGHT_BLOCKED',
          message: 'A launch gate changed after the ticket check.',
          gate_id: 'broker.connection',
          blockers: [
            {
              condition: {
                id: 'broker_disconnected',
                severity: 'blocking',
                scope: 'broker',
                evidence: { observed: false },
              },
              host: 'deploy_preflight',
              disposition: 'fix_elsewhere',
              headline: 'Broker session needs reconnecting',
              detail: 'Reconnect through Account Clerk, then retry the launch.',
              primary_move: {
                label: 'Open broker account',
                action: { kind: 'navigate', route: '/broker/account-monitor', fragment: null },
                target: null,
              },
              secondary_moves: [],
              applies_to: 'deploy',
            },
          ],
        },
      },
    });

    const e = toOperationError('deploy', err);

    expect(e.blockers).toHaveLength(1);
    expect(e.blockers?.[0]).toMatchObject({
      headline: 'Broker session needs reconnecting',
      primary_move: { label: 'Open broker account' },
    });
  });

  it('keeps typed 503 reason, gate, and server remediation instead of applying deploy-specific advice', () => {
    const err = new HttpErrorResponse({
      status: 503,
      error: {
        detail: {
          reason_code: 'FLEET_CONTAMINATION_UNAVAILABLE',
          message: 'Fleet contamination status cannot be verified.',
          remediation: 'Restore fleet inspection, then retry launch.',
          gate_id: 'fleet.contamination',
        },
      },
    });

    const e = toOperationError('deploy', err);

    expect(e.category).toBe('infra');
    expect(e.reason_code).toBe('FLEET_CONTAMINATION_UNAVAILABLE');
    expect(e.gate_id).toBe('fleet.contamination');
    expect(e.remediation).toBe('Restore fleet inspection, then retry launch.');
    expect(e.remediation).not.toContain('Start the live engine');
  });

  it('falls back to the legacy string-detail path when the 409 body is not OUTCOME_UNKNOWN', () => {
    // A regular precondition 409 (e.g. dirty tree) still uses the canned
    // remediation, NOT the new outcome-unknown branch.
    const err = new HttpErrorResponse({ status: 409, error: { detail: 'dirty tree' } });

    const e = toOperationError('deploy', err);

    expect(e.category).toBe('precondition');
    expect(e.detail).toBe('dirty tree');
    expect(e.remediation).not.toContain('Refresh the cockpit');
  });
});
