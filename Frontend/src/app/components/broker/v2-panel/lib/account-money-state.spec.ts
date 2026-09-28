import { HttpErrorResponse } from '@angular/common/http';
import { describe, expect, it } from 'vitest';

import { fakeAccountMoney, unavailableAccountMoney } from '../../../../testing/account-money-fixtures';
import {
  accountMoneyState,
  MONEY_FAILED_NEXT_STEP,
  MONEY_FAILED_TEXT,
  MONEY_INCAPABLE_TEXT,
  MONEY_LOADING_TEXT,
} from './account-money-state';

/** The 503 the money route raises when the clerk cannot serve custody
 * (`_raise_alpaca_deploy_error` over `budget_deploy.money_error`). */
function refusal503(): HttpErrorResponse {
  return new HttpErrorResponse({
    status: 503,
    error: {
      detail: {
        outcome: 'blocked',
        message: 'This account’s money cannot be read right now.',
        why: 'The account’s records are still being opened.',
        next_action: 'Open the account’s Settings to see why, then retry.',
      },
    },
  });
}

describe('accountMoneyState', () => {
  it('draws a ready answer exactly as the backend authored it', () => {
    const view = fakeAccountMoney();

    expect(accountMoneyState({ capable: true, view, error: undefined })).toEqual({ kind: 'ready', view });
  });

  it('keeps the last answer through a reload rather than blanking it', () => {
    const view = fakeAccountMoney();

    // A resource that is reloading still reports its value, and no error.
    expect(accountMoneyState({ capable: true, view, error: undefined }).kind).toBe('ready');
  });

  it.each(['unavailable', 'legacy'] as const)('says an answered %s state in the backend’s own words, never $0', (state) => {
    const view = unavailableAccountMoney('No daily loss limit is set for this account. Set one in Settings.', state);

    expect(accountMoneyState({ capable: true, view, error: undefined })).toEqual({
      kind: 'unavailable',
      view,
      text: 'No daily loss limit is set for this account. Set one in Settings.',
      nextStep: null,
    });
  });

  it('surfaces a refusal’s message, why and next step instead of discarding them', () => {
    expect(accountMoneyState({ capable: true, view: undefined, error: refusal503() })).toEqual({
      kind: 'unavailable',
      view: null,
      text: 'This account’s money cannot be read right now. The account’s records are still being opened.',
      nextStep: 'Open the account’s Settings to see why, then retry.',
    });
  });

  it('reads the fleet’s flat refusal body too', () => {
    const error = new HttpErrorResponse({
      status: 503,
      error: { reason: 'LANE_UNREACHABLE', message: 'This account’s connection is not answering.', next_step: 'Check the connection in Settings.' },
    });

    expect(accountMoneyState({ capable: true, view: undefined, error })).toEqual({
      kind: 'unavailable',
      view: null,
      text: 'This account’s connection is not answering.',
      nextStep: 'Check the connection in Settings.',
    });
  });

  it('still names a next step when the failure carried no words of its own', () => {
    expect(accountMoneyState({ capable: true, view: undefined, error: new Error('network down') })).toEqual({
      kind: 'unavailable',
      view: null,
      text: MONEY_FAILED_TEXT,
      nextStep: MONEY_FAILED_NEXT_STEP,
    });
  });

  it('is loading while nothing has answered yet, including while the lane is unresolved', () => {
    for (const capable of [true, null]) {
      expect(accountMoneyState({ capable, view: undefined, error: undefined })).toEqual({
        kind: 'loading',
        text: MONEY_LOADING_TEXT,
        nextStep: null,
      });
    }
  });

  it('says a lane that serves no money read has none, rather than reporting a failure', () => {
    expect(accountMoneyState({ capable: false, view: undefined, error: new Error('never asked') })).toEqual({
      kind: 'incapable',
      text: MONEY_INCAPABLE_TEXT,
      nextStep: null,
    });
  });
});
