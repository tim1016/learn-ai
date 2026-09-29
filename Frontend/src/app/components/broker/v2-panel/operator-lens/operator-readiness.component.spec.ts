/** The Checks fold's body (#2563). These cases moved here from the retired
 * Operator lens spec: the gate list, the header action kept out of it, and a
 * blocker's own cure dispatched through the same `actionRequested` output. */
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type {
  ActionId,
  BotPanelView,
  PanelAction,
  ReadinessCheckView,
} from '../lib/broker-v2-panel.types';
import { BOT_COCKPIT_RECONCILE_ANCHOR } from '../../../../api/operator-blocker.types';
import type { OperatorBlocker } from '../../../../api/operator-blocker.types';
import { fakeBotPanelView, fakePanelAction } from '../../../../testing/bot-panel-fixtures';
import { OperatorReadinessComponent } from './operator-readiness.component';

function check(action: PanelAction, overrides: Partial<ReadinessCheckView> = {}): ReadinessCheckView {
  return {
    operation: action.action_id,
    label: action.label,
    ready: action.enabled,
    scope: 'bot',
    authority: 'Bot lifecycle registry',
    explanation: action.explanation,
    evidence: {},
    evaluated_at_ms: 1_700_000_001_000,
    cure: null,
    ...overrides,
  };
}

function panelWith(actions: PanelAction[], checks: ReadinessCheckView[]): BotPanelView {
  return fakeBotPanelView({
    actions,
    readiness_checks: checks,
    readiness_ready_count: checks.filter((c) => c.ready).length,
    readiness_blocked_count: checks.filter((c) => !c.ready).length,
  });
}

async function renderChecks(panel: BotPanelView, bannerActionId: ActionId | null = null) {
  const actionRequested = vi.fn();
  await render(OperatorReadinessComponent, {
    inputs: { panel, bannerActionId, actionPending: false },
    on: { actionRequested },
  });
  return { actionRequested };
}

function expand(label: string): void {
  const header = screen.getByRole('button', { name: new RegExp(`(?:Ready|Blocked) ${label}`) });
  if (header.getAttribute('aria-expanded') !== 'true') fireEvent.click(header);
}

describe('OperatorReadinessComponent', () => {
  it('says so when the backend reports no checks', async () => {
    await renderChecks(panelWith([], []));

    expect(screen.getByText('No checks reported for this bot.')).toBeTruthy();
  });

  it('renders only the list: the fold summary is its heading', async () => {
    const stop = fakePanelAction('stop');
    await renderChecks(panelWith([stop], [check(stop)]));

    expect(screen.queryByRole('heading')).toBeNull();
    expect(screen.queryByText(/command gates/i)).toBeNull();
  });

  it('keeps the header action out of the list while its gate stays visible', async () => {
    const stop = fakePanelAction('stop', { explanation: 'Stop this bot.' });
    const { actionRequested } = await renderChecks(panelWith([stop], [check(stop)]), 'stop');

    expand('Stop');
    expect(screen.getByText('Stop this bot.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
    expect(actionRequested).not.toHaveBeenCalled();
  });

  it('offers a gate’s command beside it and passes a press up', async () => {
    const flatten = fakePanelAction('execute_safe_flatten', { label: 'Execute safe flatten', explanation: '' });
    const { actionRequested } = await renderChecks(panelWith([flatten], [check(flatten)]));

    expand('Execute safe flatten');
    fireEvent.click(await screen.findByRole('button', { name: 'Execute safe flatten' }));

    expect(actionRequested).toHaveBeenCalledWith({ action: flatten, reason: null });
  });

  it('keeps a disabled command’s reason code visible with its gate', async () => {
    const blockedExplanation = "Stop the bot's active run before executing a recovery flatten.";
    const flatten = fakePanelAction('execute_safe_flatten', {
      label: 'Execute safe flatten',
      explanation: blockedExplanation,
      enabled: false,
      blockers: [
        {
          condition: { id: 'RUN_STILL_ACTIVE', severity: 'blocking', scope: 'bot' },
          host: 'bot_cockpit',
          anchor: { kind: 'surface', subject_key: null },
          disposition: 'wait',
          headline: blockedExplanation,
          detail: 'Stop bot decisions first, then execute the prepared flatten.',
          primary_move: null,
          secondary_moves: [],
          applies_to: 'run',
        },
      ],
    });
    await renderChecks(
      panelWith([flatten], [check(flatten, { cure: 'Stop bot decisions first, then execute the prepared flatten.' })]),
    );

    expect(screen.getAllByText(blockedExplanation)).toHaveLength(1);
    expand('Execute safe flatten');
    expect(await screen.findByRole('button', { name: 'Execute safe flatten' })).toBeTruthy();
    expect(
      screen.getAllByRole('alert').some((alert) => alert.textContent?.includes('Run Still Active')),
    ).toBe(true);
  });

  describe('a blocker that names its own cure', () => {
    const reconcile = fakePanelAction('reconcile_now', {
      label: 'Reconcile now',
      explanation: 'Refresh the account’s order records.',
    });

    function staleEvidence(disposition: OperatorBlocker['disposition']): OperatorBlocker {
      return {
        condition: { id: 'RECOVERY_EVIDENCE_STALE', severity: 'blocking', scope: 'account', evidence: {} },
        host: 'bot_cockpit',
        anchor: { kind: 'surface', subject_key: null },
        disposition,
        headline: 'Order records for this account are out of date.',
        detail: 'Reconcile to refresh them.',
        primary_move: disposition === 'wait'
          ? null
          : {
              label: 'Reconcile this account now',
              action: { kind: 'confirm_in_form', anchor: BOT_COCKPIT_RECONCILE_ANCHOR },
              target: null,
            },
        secondary_moves: [],
        applies_to: 'run',
      };
    }

    function stalePanel(disposition: OperatorBlocker['disposition'], actions: PanelAction[] = [reconcile]): BotPanelView {
      const recover = fakePanelAction('recover_exact_execution_evidence', {
        label: 'Recover execution evidence',
        explanation: 'Recover the exact execution evidence for this run.',
        enabled: false,
        blockers: [staleEvidence(disposition)],
      });
      return panelWith([recover, ...actions], [check(recover)]);
    }

    it('dispatches the command a fix-here blocker names', async () => {
      const { actionRequested } = await renderChecks(stalePanel('fix_here'));

      expand('Recover execution evidence');
      fireEvent.click(await screen.findByRole('button', { name: 'Reconcile this account now' }));

      expect(actionRequested).toHaveBeenCalledWith({ action: reconcile, reason: null });
    });

    it('offers no cure for a wait blocker', async () => {
      await renderChecks(stalePanel('wait'));

      expand('Recover execution evidence');
      expect(screen.queryByRole('button', { name: 'Reconcile this account now' })).toBeNull();
    });

    it('offers no cure when the panel presents no command to run', async () => {
      await renderChecks(stalePanel('fix_here', []));

      expand('Recover execution evidence');
      expect(screen.queryByRole('button', { name: 'Reconcile this account now' })).toBeNull();
    });
  });
});
