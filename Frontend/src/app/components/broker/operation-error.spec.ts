import { HttpErrorResponse } from '@angular/common/http';
import { describe, expect, it } from 'vitest';
import { describeOperationError, toOperationError } from './operation-error';

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
    expect(e.remediation.toLowerCase()).toContain('host daemon');
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
});
