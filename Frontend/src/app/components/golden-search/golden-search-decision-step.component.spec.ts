import { provideRouter } from '@angular/router';
import { fireEvent, render, screen, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { GoldenSearchDecisionStepComponent } from './golden-search-decision-step.component';
import type { StudyStep } from './golden-search-steps';
import type { ExamView, StudyCommand, StudyDetail } from './golden-search.types';
import { emaCapability, examView, protocol, studyDetail } from './testing/fixtures';

async function renderStep(study: StudyDetail) {
  const view = await render(GoldenSearchDecisionStepComponent, {
    inputs: { study, capability: emaCapability() },
    providers: [provideRouter([])],
  });
  const commands: StudyCommand[] = [];
  const steps: StudyStep[] = [];
  view.fixture.componentInstance.studyCommand.subscribe((command) => commands.push(command));
  view.fixture.componentInstance.goTo.subscribe((step) => steps.push(step));
  return { view, commands, steps };
}

function reviewWith(exam: Partial<ExamView>, overrides: Partial<StudyDetail> = {}): StudyDetail {
  const base = studyDetail('awaiting_review');
  return { ...base, results: { ...base.results, exam: examView(exam) }, ...overrides };
}

function approveButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: /approve golden configuration|retry qualification/i }) as HTMLButtonElement;
}

function reason(): HTMLTextAreaElement {
  return screen.getByLabelText('Reason for this decision') as HTMLTextAreaElement;
}

async function noAxeViolations(container: Element): Promise<void> {
  const results = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
  expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`)).toEqual([]);
}

describe('GoldenSearchDecisionStepComponent — before the final test', () => {
  it('shows what one look consumes and the rules it is judged by, and opens only after the acknowledgement', async () => {
    const { commands, steps, view } = await renderStep(studyDetail('candidate_locked'));

    expect(screen.getByRole('heading', { name: '2026-01-01 – 2026-03-31 is still sealed' })).not.toBeNull();
    const lock = screen.getByRole('region', { name: /is still sealed/i });
    expect(lock.textContent).toContain('Only All-period fit and the frozen incumbent will be evaluated.');
    expect(lock.textContent).toMatch(/Development\s*2024-01-01\s*–\s*2025-12-31\s*· used to choose/);
    expect(lock.textContent).toMatch(/Worst drawdown \/ activity\s*≤ 20% \/ ≥ 30 trades/);
    expect(lock.textContent).toMatch(/Incumbent comparison\s*At least the incumbent's Sharpe Ratio/);
    const open = screen.getByRole('button', { name: 'Open final test' }) as HTMLButtonElement;
    expect(open.disabled).toBe(true);

    fireEvent.click(screen.getByRole('checkbox', { name: 'I understand this locks my candidate and consumes this test interval.' }));
    expect(open.disabled).toBe(false);
    fireEvent.click(open);
    fireEvent.click(screen.getByRole('button', { name: 'Return to candidates' }));

    expect(commands).toEqual([{ command: 'open_exam', payload: { acknowledge_final_test: true } }]);
    expect(steps).toEqual(['compare']);
    await noAxeViolations(view.container);
  });

  it('a lock the server will not open stays disabled and says why', async () => {
    await renderStep(studyDetail('candidate_locked', { permitted_actions: ['select_candidate'], action_refusals: { open_exam: 'Another study is opening this interval.' } }));

    fireEvent.click(screen.getByRole('checkbox', { name: /locks my candidate/i }));

    expect((screen.getByRole('button', { name: 'Open final test' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('Another study is opening this interval.')).not.toBeNull();
  });

  it('without a locked candidate points back to Compare', async () => {
    const { steps } = await renderStep(studyDetail('awaiting_candidate'));
    fireEvent.click(screen.getByRole('button', { name: 'Return to candidates' }));
    expect(steps).toEqual(['compare']);
  });

  it('while the test runs it says what is running, on which dates', async () => {
    await renderStep(studyDetail('exam_running'));
    expect(screen.getByRole('region', { name: 'Final test running' }).textContent).toMatch(/Only All-period fit and the frozen incumbent run on\s*2026-01-01\s*–\s*2026-03-31/);
  });
});

describe('GoldenSearchDecisionStepComponent — after the final test', () => {
  it('shows the exact settings, every parameter and fixed value, and what they replace', async () => {
    await renderStep(studyDetail('awaiting_review'));

    const tuple = screen.getByRole('region', { name: 'The exact settings you are approving' });
    expect(tuple.textContent).toContain('program version ema-crossover-signal/v3');
    expect(tuple.textContent).toContain('Gap $0.15 · RSI 48–72 · EMA 8/21 · hold 4 bars');
    const params = within(tuple).getByLabelText('Every parameter');
    expect(params.textContent).toMatch(/Fast EMA length\s*decision bars\s*8/);
    expect(params.textContent).toMatch(/Crossover gap \(bps\)\s*basis points\s*0\s*default/);
    expect(within(tuple).getByRole('list', { name: 'Fixed values' }).textContent).toMatch(/Normalized gap fixed at 0 bps\s*RSI length 14\s*Decision cadence 15 minutes/);
    expect(tuple.textContent).toMatch(/Replaces the default: Gap \$0\.20 · RSI 50–70 · EMA 5\/10 · hold 5 bars; fixed values unchanged\.\s*No bot starts on approval\./);
  });

  it('reads the final test against the frozen incumbent, with each check and the four separate evidence rows', async () => {
    await renderStep(studyDetail('awaiting_review'));

    const result = screen.getByRole('region', { name: 'Final test result' });
    const rows = within(result).getAllByRole('row');
    expect(rows[0].textContent).toMatch(/2026-01-01\s*–\s*2026-03-31/);
    expect(rows[1].textContent).toMatch(/All-period fit\s*\+1\.8%\s*3\.1%\s*36\s*0\.91/);
    expect(rows[2].textContent).toMatch(/Frozen incumbent\s*\+0\.9%\s*4\.2%\s*39\s*0\.54/);
    expect(within(result).getByRole('list', { name: 'Final-test checks' }).textContent).toContain('Pass · At least the incumbent — Sharpe 0.91 against 0.54.');
    expect(result.textContent).toContain('61.0% — descriptive only, never a check');
    expect(screen.getByRole('region', { name: 'Research evidence' }).textContent).toContain('Meets the stated rules');
    expect(screen.getByRole('region', { name: 'Repeatability and coverage' }).textContent).toContain('Built during approval');
    expect(screen.getByRole('region', { name: 'Independent engine agreement' }).textContent).toContain('Missing · Python-only');
    expect(screen.getByRole('region', { name: 'Golden acceptance' }).textContent).toContain('Awaiting your approval');
  });

  it('a confirmatory test that met the rules prefills a factual reason; the parity acknowledgement starts unchecked and gates approval', async () => {
    const { commands, view } = await renderStep(studyDetail('awaiting_review'));

    expect(reason().value).toContain('meets the stated rules on a confirmatory final test');
    const parity = screen.getByRole('checkbox', { name: /i accept missing independent engine agreement for program ema-crossover-signal\/v3/i }) as HTMLInputElement;
    expect(parity.checked).toBe(false);
    expect(screen.queryByRole('checkbox', { name: /i approve despite/i })).toBeNull();
    expect(approveButton().disabled).toBe(true);

    fireEvent.click(parity);
    expect(approveButton().disabled).toBe(false);
    fireEvent.input(reason(), { target: { value: '   ' } });
    expect(approveButton().disabled).toBe(true);
    expect(screen.getByText('Write the reason for this decision.')).not.toBeNull();
    fireEvent.input(reason(), { target: { value: 'Better Sharpe on an untouched quarter.' } });
    fireEvent.click(approveButton());

    expect(commands).toEqual([
      { command: 'approve', payload: { note: 'Better Sharpe on an untouched quarter.', acknowledge_missing_parity: true, acknowledge_research_weakness: false, expected_default_qualification_id: null } },
    ]);
    await noAxeViolations(view.container);
  });

  it('weak evidence needs its own acknowledgement naming the weakness, and nothing is prefilled', async () => {
    const qualified = protocol({ incumbent: { source: 'qualification', qualification_id: 'gq-prev-0001', params: { gap: 0.2, symbol: 'SPY' } } });
    const study = reviewWith(
      { outcome: 'not_enough_evidence', claim: 'exploratory', exposure_state: 'previously_used', checks: [{ code: 'SAMPLE_FLOOR', label: 'Enough trades', status: 'fail', detail: '12 trades against 30.' }] },
      { protocol: qualified },
    );
    const { commands } = await renderStep(study);

    expect(reason().value).toBe('');
    expect(screen.getByRole('region', { name: 'Research evidence' }).textContent).toMatch(/Not enough evidence · Previously used, exploratory\s*12 trades against 30\./);
    fireEvent.input(reason(), { target: { value: 'Owner accepts the thin sample.' } });
    fireEvent.click(screen.getByRole('checkbox', { name: /missing independent engine agreement/i }));
    expect(approveButton().disabled).toBe(true);
    expect(screen.getByText('Acknowledge the weak research evidence separately.')).not.toBeNull();

    fireEvent.click(screen.getByRole('checkbox', { name: 'I approve despite not enough final-test evidence and the previously used test interval. Preserve this evidence warning and my written reason.' }));
    fireEvent.click(approveButton());

    expect(commands).toEqual([
      { command: 'approve', payload: { note: 'Owner accepts the thin sample.', acknowledge_missing_parity: true, acknowledge_research_weakness: true, expected_default_qualification_id: 'gq-prev-0001' } },
    ]);
  });

  it('approval the server does not offer stays disabled even with every acknowledgement, and says why', async () => {
    const { commands } = await renderStep(
      reviewWith({}, { permitted_actions: ['retain', 'revise'], action_refusals: { approve: 'Approval follows the final test.' } }),
    );

    fireEvent.input(reason(), { target: { value: 'Ready to approve.' } });
    fireEvent.click(screen.getByRole('checkbox', { name: /missing independent engine agreement/i }));

    expect(approveButton().disabled).toBe(true);
    expect(screen.getByText('Approval follows the final test.')).not.toBeNull();
    fireEvent.click(approveButton());
    expect(commands).toEqual([]);
  });

  it('a failed rule names what failed in the acknowledgement', async () => {
    await renderStep(
      reviewWith({ outcome: 'does_not_meet_rules', checks: [{ code: 'NET_POSITIVE', label: 'Profit after stated costs', status: 'fail', detail: 'Net loss of $310.' }] }),
    );

    expect(screen.getByRole('checkbox', { name: /i approve despite failing the stated rules \(profit after stated costs\)/i })).not.toBeNull();
  });

  it('Keep current settings and the other finishes record the decision with the written reason', async () => {
    const { commands } = await renderStep(studyDetail('awaiting_review'));

    fireEvent.input(reason(), { target: { value: 'Not worth the change.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Keep current settings' }));
    fireEvent.click(within(screen.getByRole('group', { name: 'Other decisions' })).getByRole('button', { name: 'Wait for fresh data' }));

    expect(commands).toEqual([
      { command: 'retain', payload: { kind: 'keep_current', note: 'Not worth the change.' } },
      { command: 'retain', payload: { kind: 'wait_for_fresh_data', note: 'Not worth the change.' } },
    ]);
  });

  it('a poll of the same study keeps what the owner typed and checked', async () => {
    const { view } = await renderStep(studyDetail('awaiting_review'));
    fireEvent.input(reason(), { target: { value: 'My own words.' } });
    fireEvent.click(screen.getByRole('checkbox', { name: /missing independent engine agreement/i }));

    view.fixture.componentRef.setInput('study', studyDetail('awaiting_review', { revision: 4 }));
    await view.fixture.whenStable();

    expect(reason().value).toBe('My own words.');
    expect((screen.getByRole('checkbox', { name: /missing independent engine agreement/i }) as HTMLInputElement).checked).toBe(true);
  });

  it('while the approval runs it says the proof is being built and offers no second approval', async () => {
    await renderStep(studyDetail('qualification_pending'));

    expect(screen.getByRole('status').textContent).toContain('Building the proof and publishing the version');
    expect(screen.getByRole('progressbar', { name: 'Approval progress' })).not.toBeNull();
    expect(screen.queryByRole('button', { name: /approve golden configuration/i })).toBeNull();
    expect(screen.getByRole('region', { name: 'Repeatability and coverage' }).textContent).toContain('Building the proof');
  });

  it('a proof failure is an alert that override cannot bypass; the retry is the same approval', async () => {
    const { commands } = await renderStep(studyDetail('qualification_failed'));

    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain('Qualification could not be verified.');
    expect(alert.textContent).toContain('The restored replay did not match the lake replay (TRACE_MISMATCH).');
    expect(alert.textContent).toContain('the existing default is unchanged');
    expect(screen.getByRole('region', { name: 'Repeatability and coverage' }).textContent).toContain('Proof failed');

    fireEvent.click(screen.getByRole('checkbox', { name: /missing independent engine agreement/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Retry qualification' }));
    expect(commands.map((c) => c.command)).toEqual(['approve']);
  });

  it('approved: the golden settings, their qualification and program version, and Use in Deploy carrying the qualification', async () => {
    const { view } = await renderStep(studyDetail('approved'));

    const tuple = screen.getByRole('region', { name: 'Your golden settings' });
    expect(tuple.textContent).toContain('Qualification gq-0001-aaaa-bbbb · the new default for this strategy and instrument.');
    expect(within(tuple).getByLabelText('Every parameter').textContent).toMatch(/Hold time\s*decision bars\s*4/);
    expect(screen.getByRole('region', { name: 'Golden acceptance' }).textContent).toContain('Accepted · Manual override');
    expect(screen.getByRole('region', { name: 'Your recorded decision' }).textContent).toContain('Manual override acknowledged for program ema-crossover-signal/v3.');
    const deploy = screen.getByRole('link', { name: /use in deploy/i });
    expect(deploy.getAttribute('href')).toBe('/brokers/alpaca?golden_qualification=gq-0001-aaaa-bbbb');
    expect(screen.queryByRole('checkbox')).toBeNull();
    await noAxeViolations(view.container);
  });

  it('retained: shows the recorded decision and keeps the final-test result it was based on', async () => {
    const base = studyDetail('awaiting_review');
    const { steps } = await renderStep({
      ...base,
      state: 'retained',
      permitted_actions: ['revise'],
      decision: { kind: 'keep_current', note: 'The test did not justify a change.', at_ms: base.updated_at_ms },
    });

    const recorded = screen.getByRole('region', { name: 'Recorded decision' });
    expect(recorded.textContent).toContain('Keep current settings');
    expect(recorded.textContent).toContain('The test did not justify a change.');
    expect(screen.getByRole('region', { name: 'Final test result' })).not.toBeNull();
    expect(screen.queryByRole('button', { name: /approve/i })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Return to evidence' }));
    expect(steps).toEqual(['compare']);
  });
});
