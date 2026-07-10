import { describe, expect, it } from 'vitest';

import type {
  BotEventRow,
  TerminalErrorCode,
} from '../../../../../api/live-runs.types';
import { makeStatus } from '../../bot-control-page.fixtures';
import {
  actionCommandForRow,
  actionForRow,
  BOT_EVENT_TYPES,
  TERMINAL_ERROR_CODES,
} from './bot-event-stream-action';

describe('bot event stream action mapper', () => {
  it('keeps the closed event-type affordance map explicit', () => {
    expect(Object.fromEntries(
      BOT_EVENT_TYPES.map((eventType) => [eventType, actionCommandForRow(row({ event_type: eventType }))]),
    )).toMatchInlineSnapshot(`
      {
        "blocked": null,
        "evaluation_idle": null,
        "halted": "fresh_run",
        "launch_failed": "start_process",
        "order_cancelled": null,
        "order_filled": null,
        "order_rejected": "mark_poisoned",
        "order_submitted": null,
        "signal_fired": null,
      }
    `);
  });

  it('keeps the closed terminal-error affordance map explicit', () => {
    expect(Object.fromEntries(
      TERMINAL_ERROR_CODES.map((code) => [
        code,
        actionCommandForRow(row({
          terminal_error: {
            ...terminalError('halted'),
            code,
          },
        })),
      ]),
    )).toMatchInlineSnapshot(`
      {
        "halted": "fresh_run",
        "launch_failed": "start_process",
        "order_rejected": "mark_poisoned",
        "submit_uncertain": "flatten_and_pause",
        "unmapped_diagnostic": null,
      }
    `);
  });

  it('joins stream affordances to current backend-authored capabilities', () => {
    const status = makeStatus({ markPoisonedEnabled: false });
    const action = actionForRow(row({ event_type: 'order_rejected' }), status, false);

    expect(action).toEqual({
      command: 'mark_poisoned',
      label: 'Mark poisoned',
      enabled: false,
      disabledReason: 'No live binding — the host runner is not bound to this instance. Start a runner first.',
    });
  });
});

function row(overrides: Partial<BotEventRow> = {}): BotEventRow {
  return {
    schema_version: 1,
    seq: 1,
    ts_ms: 1_700_000_000_000,
    event_type: 'signal_fired',
    source_authority: 'engine_loop',
    identity: {
      evaluation_id: null,
      intent_id: null,
      order_ref: null,
      req_id: null,
      order_id: null,
      perm_id: null,
      exec_id: null,
    },
    severity: 'info',
    headline: 'Event',
    narrative: 'Event narrative.',
    gate_steps: [],
    terminal_error: null,
    facts: {},
    ...overrides,
  };
}

function terminalError(code: TerminalErrorCode): NonNullable<BotEventRow['terminal_error']> {
  return {
    code,
    source: 'engine',
    gate_id: null,
    message: 'Terminal event',
    detail: null,
    external_code: null,
    external_message: null,
    cause_chain: [],
    forensic_facts: {},
  };
}
