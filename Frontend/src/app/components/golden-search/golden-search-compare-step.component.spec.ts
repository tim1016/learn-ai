import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { GoldenSearchCompareStepComponent } from './golden-search-compare-step.component';
import type { StudyStep } from './golden-search-steps';
import { GoldenSearchService } from './golden-search.service';
import type { CandidateDetail, CandidateKey, StudyCommand, StudyDetail } from './golden-search.types';
import { candidateDetail, emaCapability, evidenceCandidate, evidenceView, studyDetail } from './testing/fixtures';

interface FakeService {
  candidate: ReturnType<typeof vi.fn<(id: string, key: CandidateKey) => Promise<CandidateDetail>>>;
}

function fakeService(): FakeService {
  return { candidate: vi.fn(async (_id: string, key: CandidateKey) => candidateDetail(key)) };
}

async function renderStep(study: StudyDetail = studyDetail('awaiting_candidate'), service: FakeService = fakeService()) {
  const view = await render(GoldenSearchCompareStepComponent, {
    inputs: { study, capability: emaCapability() },
    providers: [{ provide: GoldenSearchService, useValue: service }],
  });
  const commands: StudyCommand[] = [];
  const steps: StudyStep[] = [];
  view.fixture.componentInstance.studyCommand.subscribe((command) => commands.push(command));
  view.fixture.componentInstance.goTo.subscribe((step) => steps.push(step));
  return { view, commands, steps, service };
}

function candidateRow(name: RegExp): HTMLElement {
  const row = screen.getByRole('button', { name }).closest('tr');
  if (row === null) throw new Error(`no candidate row for ${name}`);
  return row;
}

function evidenceTab(name: string): HTMLElement {
  return within(screen.getByRole('group', { name: 'Candidate evidence' })).getByRole('button', { name });
}

describe('GoldenSearchCompareStepComponent', () => {
  it('leads with the server recommendation, then every candidate on the same development scope, the incumbent last', async () => {
    await renderStep();

    expect(screen.getAllByRole('note')[0].textContent).toContain('The recent fit earns more in this replay, but nearby settings lose money.');
    const table = screen.getByRole('table', { name: /development replay · same dates, \$100,000 starting capital/i });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows.map((row) => row.querySelector('strong')?.textContent)).toEqual(['All-period fit', 'Recent fit', 'Current settings']);
    expect(rows[0].textContent).toMatch(/All-period search[\s\S]*\+8\.7%\s*6\.4%\s*146\s*1\.18/);
    expect(rows[1].textContent).toMatch(/14\.8%\s*Above 12% limit/);
    expect(rows[2].textContent).toContain('Frozen incumbent');
  });

  it('candidate evidence cut short by the budget says some results read as untested; complete evidence says nothing', async () => {
    const base = studyDetail('awaiting_candidate');
    const { view } = await renderStep({ ...base, results: { ...base.results, evidence: evidenceView({ incomplete: true }) } });
    const note = /candidate evidence stopped at its evaluation budget, so some results are missing/i;

    expect(screen.getByText(note)).not.toBeNull();
    expect(screen.getAllByRole('note')[0].textContent).toContain('The recent fit earns more in this replay');
    view.fixture.componentRef.setInput('study', base);
    await view.fixture.whenStable();
    expect(screen.queryByText(note)).toBeNull();
  });

  it('opens on the all-period fit and says exactly which settings are selected', async () => {
    await renderStep();

    expect(screen.getByRole('button', { name: /all-period fit/i }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByText('Selected: All-period fit.').parentElement?.textContent).toContain(
      'Selected: All-period fit. Gap $0.15 · RSI 48–72 · EMA 8/21 · hold 4 bars. Normalized gap fixed at 0 bps.',
    );
  });

  it('picking a candidate changes the selection, its guidance, and what Review final-test lock sends', async () => {
    const { commands } = await renderStep();

    fireEvent.click(screen.getByRole('button', { name: /recent fit/i }));

    expect(candidateRow(/recent fit/i).className).toContain('selected');
    expect(screen.getByRole('heading', { name: 'The extra return comes with a warning' })).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Review final-test lock' }));
    expect(commands).toEqual([{ command: 'select_candidate', payload: { candidate_key: 'recent' } }]);
  });

  it('the incumbent cannot open the final test: the action is disabled and says to keep the current settings instead', async () => {
    const { commands } = await renderStep();

    fireEvent.click(screen.getByRole('button', { name: /current settings frozen incumbent/i }));

    const review = screen.getByRole('button', { name: 'Review final-test lock' }) as HTMLButtonElement;
    expect(review.disabled).toBe(true);
    expect(screen.getByText(/the incumbent is not a new candidate\. use “keep current settings” to finish without consuming the test\./i)).not.toBeNull();
    expect(screen.getByRole('heading', { name: 'No change can be the best decision' })).not.toBeNull();
    expect(commands).toEqual([]);
  });

  it('folds candidates that are the same settings into one row, and a fit equal to the incumbent becomes the incumbent', async () => {
    const study = studyDetail('awaiting_candidate', {
      results: {
        ...studyDetail('awaiting_candidate').results,
        evidence: evidenceView({
          candidates: [
            evidenceCandidate('incumbent', { same_as: ['all_period'] }),
            evidenceCandidate('all_period', { same_as: ['incumbent'] }),
            evidenceCandidate('recent'),
          ],
        }),
      },
    });
    await renderStep(study);

    const rows = within(screen.getByRole('table', { name: /development replay/i })).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[1].textContent).toContain('Current settings');
    expect(rows[1].textContent).toContain('same settings as All-period fit');
    expect(screen.getByRole('button', { name: /^current settings/i }).getAttribute('aria-pressed')).toBe('true');
    expect((screen.getByRole('button', { name: 'Review final-test lock' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('a candidate already locked goes straight to its final-test lock without sending the pick again', async () => {
    const { commands, steps } = await renderStep(studyDetail('candidate_locked'));

    fireEvent.click(screen.getByRole('button', { name: 'Review final-test lock' }));

    expect(commands).toEqual([]);
    expect(steps).toEqual(['decision']);
  });

  it('a locked pick folded into another row still goes to its lock, never re-sending a different pick', async () => {
    const study = studyDetail('candidate_locked', {
      candidate_key: 'recent',
      results: {
        ...studyDetail('candidate_locked').results,
        evidence: evidenceView({
          candidates: [
            evidenceCandidate('all_period', { same_as: ['recent'] }),
            evidenceCandidate('recent', { same_as: ['all_period'] }),
            evidenceCandidate('incumbent'),
          ],
        }),
      },
    });
    const { commands, steps } = await renderStep(study);

    expect(screen.getByRole('button', { name: /^all-period fit/i }).getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(screen.getByRole('button', { name: 'Review final-test lock' }));

    expect(commands).toEqual([]);
    expect(steps).toEqual(['decision']);
  });

  it('after the final test opened, the pick is fixed and the note says its result is recorded separately', async () => {
    const { steps } = await renderStep(studyDetail('awaiting_review'));

    expect((screen.getByRole('button', { name: /recent fit/i }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/final test is recorded separately\. its development results remain in-sample/i)).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Review final decision' }));
    expect(steps).toEqual(['decision']);
  });

  it('shows what choosing the candidate implies beside the frozen rules', async () => {
    await renderStep();

    const aside = screen.getByRole('complementary');
    expect(aside.textContent).toMatch(/Your loss ceiling\s*20%/);
    expect(aside.textContent).toMatch(/Sample floor\s*30 trades/);
    expect(aside.textContent).toMatch(/Final test\s*Still locked/);
    expect(aside.textContent).toMatch(/Luck adjustment\s*Not estimable/);
  });

  it('Equity reads every candidate’s detail run once and draws their cumulative returns, with a table alternative', async () => {
    const { service } = await renderStep();

    expect(service.candidate).not.toHaveBeenCalled();
    fireEvent.click(evidenceTab('Equity'));

    const chart = await screen.findByRole('img', { name: /development cumulative return — ends at all-period fit \+8\.7%, recent fit \+11\.6%, current settings \+5\.2%/i });
    expect(chart).not.toBeNull();
    expect(service.candidate.mock.calls.map(([, key]) => key)).toEqual(['all_period', 'recent', 'incumbent']);
    expect(screen.getByText(/these are not the walk-forward procedure's test returns/i)).not.toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /show the daily values as a table/i }));
    const table = screen.getByRole('table', { name: /development cumulative return by session/i });
    expect(within(table).getAllByRole('row')[3].textContent).toMatch(/2025-12-31\s*\+8\.7%\s*\+11\.6%\s*\+5\.2%/);

    fireEvent.click(evidenceTab('By month'));
    fireEvent.click(evidenceTab('Equity'));
    expect(service.candidate).toHaveBeenCalledTimes(3);
  });

  it('By month and Trades show the selected candidate’s development months and decisions', async () => {
    await renderStep();

    fireEvent.click(evidenceTab('By month'));
    const months = await screen.findByRole('table', { name: /net result by month for all-period fit/i });
    expect(within(months).getAllByRole('row')[2].textContent).toMatch(/2025-12-01\s*-0\.7%\s*-\$700\s*9/);

    fireEvent.click(evidenceTab('Trades'));
    const trades = screen.getByRole('table', { name: /development trades of all-period fit/i });
    const first = within(trades).getAllByRole('row')[1].textContent ?? '';
    expect(first).toMatch(/2025-12-23/);
    expect(first).toContain('Entry at $590.10 · RSI 58.2');
    expect(first).toContain('Hold Complete · Exit at $592.50');
    // The server's trade P&L is price change times quantity: it is labelled before fees, never net.
    expect(within(trades).getByRole('columnheader', { name: 'P&L before fees' })).toBeTruthy();
    expect(within(trades).queryByRole('columnheader', { name: /net p&l/i })).toBeNull();
    expect(first).toContain('+$243.00');
  });

  it('a trade worth less than a dollar keeps its cents, and a month without a defined return reads — with no bar', async () => {
    const service = fakeService();
    service.candidate.mockImplementation(async (_id: string, key: CandidateKey) => {
      const detail = candidateDetail(key);
      const development = detail.development;
      if (development === null) return detail;
      return {
        ...detail,
        development: {
          ...development,
          monthly: [{ month_start_ms: development.monthly[0].month_start_ms, net_profit: -40, return_fraction: null, trades: 1 }],
          trades: [{ ...development.trades[0], quantity: 1, pnl: 0.4 }],
        },
      };
    });
    await renderStep(studyDetail('awaiting_candidate'), service);

    fireEvent.click(evidenceTab('By month'));
    const months = await screen.findByRole('table', { name: /net result by month for all-period fit/i });
    const month = within(months).getAllByRole('row')[1];
    expect(month.textContent).toMatch(/2025-11-01\s*—\s*-\$40\s*1/);
    expect(month.querySelector<HTMLElement>('.bar')?.style.width).toBe('0%');

    fireEvent.click(evidenceTab('Trades'));
    const trades = screen.getByRole('table', { name: /development trades of all-period fit/i });
    expect(within(trades).getAllByRole('row')[1].textContent).toContain('+$0.40');
  });

  it('a detail read that fails says so and can be retried', async () => {
    const service = fakeService();
    service.candidate.mockRejectedValueOnce(new Error('down'));
    await renderStep(studyDetail('awaiting_candidate'), service);

    fireEvent.click(evidenceTab('Trades'));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('could not be loaded');
    fireEvent.click(within(alert).getByRole('button', { name: 'Try again' }));

    expect(await screen.findByRole('table', { name: /development trades of all-period fit/i })).not.toBeNull();
  });

  it('Neighborhood and Stress keep tested, invalid and center values apart with their actual metrics', async () => {
    await renderStep();

    fireEvent.click(evidenceTab('Neighborhood'));
    const hood = screen.getByRole('table', { name: 'Fast EMA length' });
    const rows = within(hood).getAllByRole('row').slice(1).map((row) => row.textContent ?? '');
    expect(rows[0]).toMatch(/7\s*Tested\s*\+5\.2%/);
    expect(rows[1]).toMatch(/8\s*This candidate\s*\+8\.7%/);
    expect(rows[2]).toMatch(/9\s*Invalid\s*— untested: outside the legal domain\s*—/);

    fireEvent.click(evidenceTab('Stress'));
    expect(screen.getByRole('table', { name: /cost stress for all-period fit/i }).textContent).toMatch(/Extra 1¢\/share slippage\s*\+4\.1%/);
  });

  it('Keep current settings opens a deliberate second step and records the chosen finish with its reason', async () => {
    const { commands } = await renderStep();

    fireEvent.click(screen.getByRole('button', { name: 'Keep current settings' }));
    fireEvent.click(screen.getByRole('radio', { name: /wait for fresh data/i }));
    fireEvent.input(screen.getByLabelText(/reason \(recorded with the study\)/i), { target: { value: '  Too few trades to trust.  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Record decision' }));

    expect(commands).toEqual([{ command: 'retain', payload: { kind: 'wait_for_fresh_data', note: 'Too few trades to trust.' } }]);
  });

  it('before the evidence exists, says when it will', async () => {
    await renderStep(studyDetail('validation_running'));

    expect(screen.getByRole('status').textContent).toContain('when the test over time finishes');
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('passes axe on the map and on the equity view', async () => {
    const { view } = await renderStep();
    const check = async (): Promise<string[]> => {
      const results = await axe.run(view.container, { rules: { 'color-contrast': { enabled: false } } });
      return results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`);
    };

    expect(await check()).toEqual([]);
    fireEvent.click(evidenceTab('Equity'));
    await waitFor(() => expect(screen.getByRole('img', { name: /development cumulative return/i })).not.toBeNull());
    expect(await check()).toEqual([]);
  });
});
