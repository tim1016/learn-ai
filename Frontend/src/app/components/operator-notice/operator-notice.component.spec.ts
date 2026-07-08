import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';
import { OperatorNoticeComponent } from './operator-notice.component';
import type { OperatorNotice } from '../../models/operator-notice';

function makeNotice(overrides: Partial<OperatorNotice> = {}): OperatorNotice {
  return {
    code: 'runtime.market_data_stale',
    tier: 'warning',
    title: 'Market data is stale',
    message: 'No fresh bar has arrived for 92 seconds.',
    source_codes: ['BAR_LOOP_LATEST_BAR_STALE'],
    forensic_facts: { age_ms: 92_000 },
    actionability: 'routed',
    resolution: 'Clears when a fresh IBKR bar arrives inside the freshness window.',
    remedy_status: null,
    action: { kind: 'external_manual_check', label: 'Check IBKR market data', target: 'ibkr_connection' },
    runbook_slug: 'runtime-freshness',
    occurred_at_ms: null,
    ...overrides,
  };
}

describe('OperatorNoticeComponent', () => {
  it('renders title and message verbatim from the backend', async () => {
    await render(OperatorNoticeComponent, {
      inputs: { notice: makeNotice() },
    });

    expect(screen.getByText('Market data is stale')).toBeTruthy();
    expect(screen.getByText('No fresh bar has arrived for 92 seconds.')).toBeTruthy();
    expect(screen.getByText('Clears when a fresh IBKR bar arrives inside the freshness window.')).toBeTruthy();
    expect(screen.getByText('Check elsewhere')).toBeTruthy();
  });

  it('never renders raw source_codes as primary copy', async () => {
    await render(OperatorNoticeComponent, {
      inputs: { notice: makeNotice() },
    });

    const title = screen.getByTestId('operator-notice-title');
    const message = screen.getByTestId('operator-notice-message');
    expect(title.textContent).not.toContain('BAR_LOOP_LATEST_BAR_STALE');
    expect(message.textContent).not.toContain('BAR_LOOP_LATEST_BAR_STALE');
  });

  it('renders an action button for clickable kinds', async () => {
    await render(OperatorNoticeComponent, {
      inputs: {
        notice: makeNotice({
          action: { kind: 'open_runbook', label: 'How to recover', target: 'runtime-freshness' },
        }),
      },
    });

    expect(screen.getByRole('button', { name: /how to recover/i })).toBeTruthy();
  });

  it('uses the notice runbook slug when an open-runbook action has no target', async () => {
    await render(OperatorNoticeComponent, {
      inputs: {
        notice: makeNotice({
          action: { kind: 'open_runbook', label: 'How to recover', target: null },
          runbook_slug: 'runtime-freshness',
        }),
      },
    });

    expect(screen.getByRole('button', { name: /how to recover/i })).toBeTruthy();
  });

  it('does not render a clickable action when the executable target is missing', async () => {
    await render(OperatorNoticeComponent, {
      inputs: {
        notice: makeNotice({
          action: { kind: 'focus_cockpit_action', label: 'Focus action', target: null },
        }),
      },
    });

    expect(screen.queryByRole('button', { name: /focus action/i })).toBeNull();
  });

  it('renders the control-plane lease renew action as a button', async () => {
    await render(OperatorNoticeComponent, {
      inputs: {
        notice: makeNotice({
          action: { kind: 'renew_control_plane_lease', label: 'Renew control-plane lease', target: 'daemon_lease' },
        }),
      },
    });

    expect(screen.getByRole('button', { name: /renew control-plane lease/i })).toBeTruthy();
  });

  it('emits the action when a clickable action button is clicked', async () => {
    const notice = makeNotice({
      action: { kind: 'open_runbook', label: 'How to recover', target: 'runtime-freshness' },
    });
    let captured: OperatorNotice | null = null;
    await render(OperatorNoticeComponent, {
      inputs: { notice },
      on: { actionClicked: (value: OperatorNotice) => { captured = value; } },
    });
    await screen.getByRole('button', { name: /how to recover/i }).click();
    expect(captured).toEqual(notice);
  });

  it('renders external_manual_check as an inert label, not a clickable button', async () => {
    await render(OperatorNoticeComponent, {
      inputs: {
        notice: makeNotice({
          action: { kind: 'external_manual_check', label: 'Check positions in IBKR', target: 'ibkr_positions' },
        }),
      },
    });

    // No button — the cockpit does not perform the reconciliation.
    expect(screen.queryByRole('button', { name: /check positions/i })).toBeNull();
    expect(screen.getByText('Check positions in IBKR')).toBeTruthy();
  });

  it('renders nothing visible when action.kind is none', async () => {
    await render(OperatorNoticeComponent, {
      inputs: {
        notice: makeNotice({ tier: 'info', action: { kind: 'none', label: null, target: null } }),
      },
    });

    expect(screen.queryByRole('button')).toBeNull();
  });

  it('applies a tier-aware CSS class to the root and preserves the base class', async () => {
    const { container } = await render(OperatorNoticeComponent, {
      inputs: { notice: makeNotice({ tier: 'critical' }) },
    });
    const root = container.querySelector('[data-testid="operator-notice"]');
    expect(root?.classList.contains('operator-notice')).toBe(true);
    expect(root?.classList.contains('tier-critical')).toBe(true);
  });

  it('exposes forensic facts via an expandable details element', async () => {
    await render(OperatorNoticeComponent, {
      inputs: { notice: makeNotice({ forensic_facts: { bar_loop_age_ms: 99_000, feed: 'polygon' } }) },
    });

    const details = screen.getByTestId('operator-notice-forensic-facts');
    expect(details.textContent).toContain('bar_loop_age_ms');
    expect(details.textContent).toContain('99000');
    expect(details.textContent).toContain('polygon');
  });
});
