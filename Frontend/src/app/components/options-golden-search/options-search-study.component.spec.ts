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
    const failed = candidate({ verdict: 'does_not_meet_rules', gate_outcomes: [{ gate_id: 'drawdown', status: 'failed', actual: '23%', required: '20%', reason: 'Minute-close loss exceeds the frozen limit.' }] });
    await render(OptionsSearchStudyComponent, { inputs: { study: study({ final_results: [failed], allowed_commands: ['approve', 'approve_exception', 'keep_original'] }), step: 5 }, on: { action }, providers: [provideRouter([])] });
    const normal = screen.getByRole('button', { name: 'Approve exact golden version' });
    const exception = screen.getByRole('button', { name: 'Approve owner exception' });
    expect(normal).toHaveProperty('disabled', true);
    expect(exception).toHaveProperty('disabled', true);
    expect(screen.getByText(/actual 23%; required 20%/)).toBeTruthy();
    const user = userEvent.setup();
    await user.type(screen.getByRole('textbox', { name: 'Owner review comment' }), 'Reviewed the concentration and accepted this loss ceiling.');
    await user.click(exception);
    expect(action).toHaveBeenCalledWith({ command: 'approve_exception', candidate_hash: failed.candidate_hash, comment: 'Reviewed the concentration and accepted this loss ceiling.' });
  });

  it('blocks a technical exception and renders exact money without rounding', async () => {
    const technical = candidate({ verdict: 'could_not_evaluate' });
    await render(OptionsSearchStudyComponent, { inputs: { study: study({ final_results: [technical], allowed_commands: ['approve_exception'] }), step: 5 }, providers: [provideRouter([])] });
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
