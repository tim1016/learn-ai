import { describe, expect, it, vi } from 'vitest';
import type { OperatorNotice } from '../../../../models/operator-notice';
import {
  renderOperatorNoticeAction,
  type OperatorNoticeDispatch,
} from './operator-notice-action-renderer';

function notice(overrides: Partial<OperatorNotice> = {}): OperatorNotice {
  return {
    code: 'runtime.market_data_stale',
    tier: 'warning',
    title: 'Market data is stale',
    message: 'No fresh bar has arrived.',
    source_codes: [],
    forensic_facts: {},
    action: { kind: 'wait', label: null, target: null },
    runbook_slug: null,
    occurred_at_ms: null,
    ...overrides,
  };
}

function makeDispatch(): OperatorNoticeDispatch & {
  calls: {
    openRunbook: string[];
    focusTarget: string[];
    redeploy: number;
    renewControlPlaneLease: number;
  };
} {
  const calls = {
    openRunbook: [] as string[],
    focusTarget: [] as string[],
    redeploy: 0,
    renewControlPlaneLease: 0,
  };
  return {
    redeploy: vi.fn(() => {
      calls.redeploy += 1;
    }),
    openRunbook: vi.fn((slug) => calls.openRunbook.push(slug)),
    focusTarget: vi.fn((target) => calls.focusTarget.push(target)),
    renewControlPlaneLease: vi.fn(() => {
      calls.renewControlPlaneLease += 1;
    }),
    calls,
  };
}

describe('renderOperatorNoticeAction', () => {
  it('opens a notice runbook from action target', () => {
    const dispatch = makeDispatch();
    const rendered = renderOperatorNoticeAction(
      notice({
        action: { kind: 'open_runbook', label: 'Open runbook', target: 'runtime-freshness' },
      }),
      dispatch,
    );

    rendered?.invoke();

    expect(rendered?.label).toBe('Open runbook');
    expect(dispatch.calls.openRunbook).toEqual(['runtime-freshness']);
  });

  it('falls back to notice runbook_slug for open-runbook notices', () => {
    const dispatch = makeDispatch();
    const rendered = renderOperatorNoticeAction(
      notice({
        action: { kind: 'open_runbook', label: 'Open runbook', target: null },
        runbook_slug: 'watchdog-halt',
      }),
      dispatch,
    );

    rendered?.invoke();

    expect(dispatch.calls.openRunbook).toEqual(['watchdog-halt']);
  });

  it('does not render a clickable action when required target evidence is missing', () => {
    const rendered = renderOperatorNoticeAction(
      notice({
        action: { kind: 'focus_cockpit_action', label: 'Focus lifecycle action', target: null },
      }),
      makeDispatch(),
    );

    expect(rendered).toBeNull();
  });

  it('dispatches focus, redeploy, and lease renewal through the page boundary', () => {
    const dispatch = makeDispatch();

    renderOperatorNoticeAction(
      notice({
        action: { kind: 'focus_cockpit_action', label: 'Focus stop', target: 'stop' },
      }),
      dispatch,
    )?.invoke();
    renderOperatorNoticeAction(
      notice({
        action: { kind: 'redeploy', label: 'Redeploy', target: null },
      }),
      dispatch,
    )?.invoke();
    renderOperatorNoticeAction(
      notice({
        action: {
          kind: 'renew_control_plane_lease',
          label: 'Renew control-plane lease',
          target: 'daemon_lease',
        },
      }),
      dispatch,
    )?.invoke();

    expect(dispatch.calls.focusTarget).toEqual(['stop']);
    expect(dispatch.calls.redeploy).toBe(1);
    expect(dispatch.calls.renewControlPlaneLease).toBe(1);
  });
});
