import { HttpErrorResponse } from '@angular/common/http';
import { describe, expect, it } from 'vitest';

import { clientRefusal, toConfigurationRefusal } from './broker-configuration-refusal';

function refused(status: number, body: unknown): HttpErrorResponse {
  return new HttpErrorResponse({ status, error: body, url: '/api/brokers/alpaca/configuration' });
}

describe('toConfigurationRefusal', () => {
  it('keeps every server-authored word of a typed refusal', () => {
    const refusal = toConfigurationRefusal(
      refused(409, {
        detail: {
          reason: 'revision_conflict',
          message: 'This profile changed while you were editing it.',
          next_step: 'Reload the profile and re-apply your change.',
        },
      }),
    );

    expect(refusal.reason).toBe('revision_conflict');
    expect(refusal.message).toBe('This profile changed while you were editing it.');
    expect(refusal.nextStep).toBe('Reload the profile and re-apply your change.');
    expect(refusal.status).toBe(409);
  });

  it('treats the two fence conflicts as stale writes and nothing else as one', () => {
    const staleRevision = toConfigurationRefusal(
      refused(409, { detail: { reason: 'revision_conflict', message: 'x' } }),
    );
    const staleGeneration = toConfigurationRefusal(
      refused(409, { detail: { reason: 'selection_generation_conflict', message: 'x' } }),
    );
    const archived = toConfigurationRefusal(
      refused(409, { detail: { reason: 'profile_archived', message: 'x' } }),
    );

    expect(staleRevision.stale).toBe(true);
    expect(staleGeneration.stale).toBe(true);
    expect(archived.stale).toBe(false);
  });

  it('accepts a refusal with no next step, which several 422s omit', () => {
    const refusal = toConfigurationRefusal(
      refused(422, {
        detail: { reason: 'live_envelope_invalid', message: 'loss_fraction must be finite.' },
      }),
    );

    expect(refusal.reason).toBe('live_envelope_invalid');
    expect(refusal.nextStep).toBeNull();
  });

  it('invents no reason code when the server did not send one', () => {
    const validation = toConfigurationRefusal(
      refused(422, { detail: [{ loc: ['body', 'display_name'], msg: 'field required' }] }),
    );

    expect(validation.reason).toBeNull();
    expect(validation.status).toBe(422);
    expect(validation.message).toContain('422');
  });

  it('reports a transport failure as unreached rather than as a status', () => {
    const offline = toConfigurationRefusal(refused(0, null));

    expect(offline.status).toBeNull();
    expect(offline.message).toContain('could not be reached');
    expect(offline.stale).toBe(false);
  });

  it('states a client-detected refusal without borrowing a server reason', () => {
    const refusal = clientRefusal('This profile has no revision to stage yet.', 'Save one first.');

    expect(refusal.reason).toBeNull();
    expect(refusal.stale).toBe(false);
    expect(refusal.message).toBe('This profile has no revision to stage yet.');
  });
});
