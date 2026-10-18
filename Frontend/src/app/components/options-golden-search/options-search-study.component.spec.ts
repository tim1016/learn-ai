import { render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { provideRouter } from '@angular/router';
import { describe, expect, it, vi } from 'vitest';

import { OptionsSearchStudyComponent } from './options-search-study.component';
import { candidate, study } from './testing';

function original() { return candidate({ candidate_hash: 'e'.repeat(64), label: 'Original setup', is_original: true }); }

describe('Options search evidence and approval', () => {
  it('freezes only the reduced tested shortlist while preserving original independently', async () => {
    const action = vi.fn();
    const second = candidate({ candidate_hash: 'f'.repeat(64), label: 'Candidate two' });
    await render(OptionsSearchStudyComponent, { inputs: { study: study({ candidates: [original(), candidate(), second], allowed_commands: ['open_final'] }), step: 4 }, on: { action }, providers: [provideRouter([])] });
    const user = userEvent.setup();
    await user.click(screen.getByRole('checkbox', { name: 'Include Candidate two in final test' }));
    await user.click(screen.getByRole('button', { name: 'Freeze shortlist and open final test' }));
    expect(action).toHaveBeenCalledWith({ command: 'open_final', candidate_hashes: ['c'.repeat(64)] });
  });

  it('requires an explicit nonblank exception comment and preserves failed-rule evidence', async () => {
    const action = vi.fn();
    const failed = candidate({ verdict: 'does_not_meet_rules', gate_outcomes: [{ gate_id: 'final:drawdown', status: 'does_not_meet_rules', actual: '23%', required: '20%', reason: 'Minute-close loss exceeds the frozen limit.' }] });
    await render(OptionsSearchStudyComponent, { inputs: { study: study({ final_results: [failed], approval_reviews: { [failed.candidate_hash]: { failures: failed.gate_outcomes, blocked_reason: null } }, allowed_commands: ['approve', 'approve_exception', 'keep_original'] }), step: 5 }, on: { action }, providers: [provideRouter([])] });
    const normal = screen.getByRole('button', { name: 'Approve exact golden version' });
    const exception = screen.getByRole('button', { name: 'Approve owner exception' });
    expect(normal).toHaveProperty('disabled', true);
    expect(exception).toHaveProperty('disabled', true);
    expect(screen.getByText(/actual 23%; required 20%/)).toBeTruthy();
    const user = userEvent.setup();
    await user.type(screen.getByRole('textbox', { name: 'Owner review comment' }), 'Reviewed the concentration and accepted this loss ceiling.');
    expect(exception).toHaveProperty('disabled', true);
    await user.click(screen.getByRole('checkbox', { name: 'Acknowledge final:drawdown' }));
    await user.click(exception);
    expect(action).toHaveBeenCalledWith({ command: 'approve_exception', candidate_hash: failed.candidate_hash, comment: 'Reviewed the concentration and accepted this loss ceiling.', waived_gate_ids: ['final:drawdown'] });
  });


  it('requires all scoped procedure, development, final and exposure acknowledgements and resets them for another candidate', async () => {
    const action = vi.fn();
    const first = candidate();
    const second = candidate({ candidate_hash: 'f'.repeat(64), label: 'Candidate two' });
    const failures = ['forward_0:sharpe', 'development:activity', 'final:drawdown', 'final:exposure'].map((gate_id) => ({ gate_id, status: 'not_enough_evidence', actual: 'actual evidence', required: 'frozen requirement', reason: 'This scope did not qualify.' }));
    await render(OptionsSearchStudyComponent, { inputs: { study: study({ final_results: [first, second], approval_reviews: { [first.candidate_hash]: { failures, blocked_reason: null }, [second.candidate_hash]: { failures, blocked_reason: null } }, allowed_commands: ['approve', 'approve_exception'] }), step: 5 }, on: { action }, providers: [provideRouter([])] });
    const user = userEvent.setup();
    await user.type(screen.getByRole('textbox', { name: 'Owner review comment' }), 'Explicitly reviewed all four evidence scopes.');
    const exception = screen.getByRole('button', { name: 'Approve owner exception' });
    expect(screen.getByRole('button', { name: 'Approve exact golden version' })).toHaveProperty('disabled', true);
    for (const failure of failures.slice(0, 3)) await user.click(screen.getByRole('checkbox', { name: `Acknowledge ${failure.gate_id}` }));
    expect(exception).toHaveProperty('disabled', true);
    await user.click(screen.getByRole('checkbox', { name: 'Acknowledge final:exposure' }));
    await user.click(exception);
    expect(action).toHaveBeenCalledWith({ command: 'approve_exception', candidate_hash: first.candidate_hash, comment: 'Explicitly reviewed all four evidence scopes.', waived_gate_ids: failures.map((failure) => failure.gate_id) });
    await user.selectOptions(screen.getByRole('combobox', { name: 'Exact candidate to approve' }), second.candidate_hash);
    expect(exception).toHaveProperty('disabled', true);
    expect(screen.getByRole('checkbox', { name: 'Acknowledge final:exposure' })).toHaveProperty('checked', false);
  });

  it('blocks a technical exception and renders exact money without rounding', async () => {
    const technical = candidate({ verdict: 'could_not_evaluate' });
    await render(OptionsSearchStudyComponent, { inputs: { study: study({ final_results: [technical], approval_reviews: { [technical.candidate_hash]: { failures: [], blocked_reason: 'Technical evidence cannot be overridden.' } }, allowed_commands: ['approve_exception'] }), step: 5 }, providers: [provideRouter([])] });
    const user = userEvent.setup();
    await user.type(screen.getByRole('textbox', { name: 'Owner review comment' }), 'Override');
    expect(screen.getByRole('button', { name: 'Approve owner exception' })).toHaveProperty('disabled', true);
    expect(screen.getByText('200.01234567890123456789 USD')).toBeTruthy();
  });

  it('keeps the owner shortlist when newer progress arrives for the same study', async () => {
    const first = study({ candidates: [original(), candidate()], allowed_commands: ['open_final'] });
    const view = await render(OptionsSearchStudyComponent, { inputs: { study: first, step: 4 }, providers: [provideRouter([])] });
    const user = userEvent.setup();
    await user.click(screen.getByRole('checkbox', { name: 'Include Candidate one in final test' }));
    view.fixture.componentRef.setInput('study', { ...first, revision: 5 });
    view.fixture.detectChanges();
    await waitFor(() => expect(screen.getByRole('checkbox', { name: 'Include Candidate one in final test' })).toHaveProperty('checked', false));
  });
});

it('keeps failing candidates inspectable without offering them for final selection', async () => {
  const failed = candidate({ candidate_hash: 'f'.repeat(64), label: 'Failed timing candidate', verdict: 'does_not_meet_rules', gate_outcomes: [{ gate_id: 'development:drawdown', status: 'does_not_meet_rules', actual: '25', required: '20', reason: 'Drawdown exceeds the frozen limit.' }] });
  await render(OptionsSearchStudyComponent, { inputs: { study: study({ candidates: [candidate(), failed], allowed_commands: ['open_final'] }), step: 4 }, providers: [provideRouter([])] });
  expect(screen.queryByRole('checkbox', { name: 'Include Failed timing candidate in final test' })).toBeNull();
  const user = userEvent.setup();
  await user.click(screen.getByText('1 candidates did not pass — inspect limits'));
  await screen.findByText('Failed timing candidate');
  expect(screen.getByText('Drawdown exceeds the frozen limit. (25 / 20)')).toBeTruthy();
});
