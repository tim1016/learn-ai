import { TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';

import { SUBMISSION_KEY_RE } from './alpaca-deploy-workflow.component';
import { DeployDraftStore, canonicalJson, freshDeployDraft } from './deploy-draft.store';

describe('the Deploy draft', () => {
  it('spells equal settings the same whatever order their keys were written in', () => {
    const left = canonicalJson({ symbol: 'SPY', parameters: { gap: 0.2, fast: 5 }, budget: { risk_revision: 1, amount_usd: '1000.00' } });
    const right = canonicalJson({ budget: { amount_usd: '1000.00', risk_revision: 1 }, parameters: { fast: 5, gap: 0.2 }, symbol: 'SPY' });

    expect(left).toBe(right);
    expect(canonicalJson({ parameters: { gap: 0.3 } })).not.toBe(canonicalJson({ parameters: { gap: 0.2 } }));
    expect(canonicalJson({ legs: [2, 1] })).not.toBe(canonicalJson({ legs: [1, 2] }));
  });

  it('gives every fresh draft its own submission key, never sent and never replacing a bot', () => {
    const first = freshDeployDraft();
    const second = freshDeployDraft();

    expect(first.submissionKey).not.toBe(second.submissionKey);
    expect(first.submissionKey).toMatch(SUBMISSION_KEY_RE);
    expect(first).toMatchObject({ amount: '', outcomeUnknown: false, replaces: null, editing: { what: false, how: false } });
    expect(first.settings.executionMode).toBeNull();
  });

  it('keeps one draft per account for the session', () => {
    const store = TestBed.inject(DeployDraftStore);
    const draft = { ...freshDeployDraft(), amount: '750.00' };

    store.write('alpaca\u0000clrk\u0000pa9', draft);

    expect(store.read('alpaca\u0000clrk\u0000pa9')).toBe(draft);
    expect(store.read('alpaca\u0000clrk\u0000pa10')).toBeNull();
  });
});
