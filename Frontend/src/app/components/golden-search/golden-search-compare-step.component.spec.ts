import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { MarkdownDrawerService } from '../../shared/markdown-drawer/markdown-drawer.service';
import { GoldenSearchCompareStepComponent } from './golden-search-compare-step.component';
import type { StudyStep } from './golden-search-steps';
import { GoldenSearchService } from './golden-search.service';
import type { CandidateDetail, CandidateKey, StudyCommand, StudyDetail } from './golden-search.types';
import { fakeCharts } from './testing/fake-charts';
import { candidateDetail, decisionSummary, emaCapability, evidenceCandidate, evidenceView, frequencyProtocol, studyDetail, tradeActivity } from './testing/fixtures';

interface FakeService {
  candidate: ReturnType<typeof vi.fn<(id: string, key: CandidateKey) => Promise<CandidateDetail>>>;
}

function fakeService(): FakeService {
  return { candidate: vi.fn(async (_id: string, key: CandidateKey) => candidateDetail(key)) };
}

async function renderStep(study: StudyDetail = studyDetail('awaiting_candidate'), service: FakeService = fakeService()) {
  const charts = fakeCharts();
  const view = await render(GoldenSearchCompareStepComponent, {
    inputs: { study, capability: emaCapability() },
    providers: [{ provide: GoldenSearchService, useValue: service }, ...charts.providers],
  });
  const commands: StudyCommand[] = [];
  const steps: StudyStep[] = [];
  view.fixture.componentInstance.studyCommand.subscribe((command) => commands.push(command));
  view.fixture.componentInstance.goTo.subscribe((step) => steps.push(step));
  return { view, commands, steps, service, charts: charts.charts };
}

function candidateCard(name: RegExp): HTMLElement {
  const card = screen.getByRole('button', { name }).closest('li');
  if (card === null) throw new Error(`no candidate card for ${name}`);
  return card;
}

function cards(): HTMLElement[] {
  return within(screen.getByRole('list', { name: 'Candidates to compare' })).getAllByRole('listitem');
}

function region(name: string): HTMLElement {
  return screen.getByRole('region', { name });
}

function evidenceTab(name: string): HTMLElement {
  return within(screen.getByRole('group', { name: 'Candidate evidence' })).getByRole('button', { name });
}

describe('GoldenSearchCompareStepComponent', () => {
  it('leads with the server recommendation, then a card for every candidate on the same development scope, the incumbent last', async () => {
    await renderStep();

    expect(screen.getAllByRole('note')[0].textContent).toContain('The recent fit earns more in this replay, but nearby settings lose money.');
    expect(region('Candidate cards').textContent).toContain('Development replay · same dates, $100,000 starting capital');
    const [allPeriod, recent, incumbent] = cards();
    expect(cards().map((card) => card.querySelector('strong')?.textContent)).toEqual(['All-period fit', 'Recent fit', 'Current settings']);
    expect(allPeriod.textContent).toMatch(/All-period search[\s\S]*Passes the rules[\s\S]*Net return\s*\+8\.7%\s*Sharpe\s*1\.18\s*Worst fall\s*6\.4%\s*Trades\s*146\s*minimum 30/);
    expect(recent.textContent).toMatch(/Fails a rule[\s\S]*14\.8%\s*Above 12% limit/);
    expect(incumbent.textContent).toContain('Frozen incumbent');
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

  it("shows the selected candidate's decision summary, each status in words, and follows its links to the evidence", async () => {
    const { steps } = await renderStep();

    const summary = screen.getByRole('region', { name: 'What the research supports' });
    const items = within(summary).getAllByRole('listitem').map((item) => item.textContent?.replace(/\s+/g, ' ').trim() ?? '');
    expect(items[0]).toMatch(/^Meets\s*Development activity\s*42 trades/);
    expect(items.find((item) => item.includes('Concentration'))).toMatch(/^Meets\s*Concentration\s*Still profitable without its best month/);
    expect(items.find((item) => item.includes('Neighbor sensitivity'))).toMatch(/^Concern/);

    fireEvent.click(within(summary).getByRole('button', { name: 'See the evidence for Neighbor sensitivity' }));
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Neighbor tornado' }));
    // A chart inside a tab: the link opens the tab, then moves focus to the chart once it shows.
    await screen.findByRole('img', { name: /development cumulative return and fall from peak/i });
    fireEvent.click(within(summary).getByRole('button', { name: 'See the evidence for Concentration' }));
    expect(evidenceTab('By month').getAttribute('aria-pressed')).toBe('true');
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Without its best' })));
    fireEvent.click(within(summary).getByRole('button', { name: 'See the evidence for Development activity' }));
    expect(evidenceTab('Trades').getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(within(summary).getByRole('button', { name: 'See the evidence for Test over time' }));
    expect(steps).toEqual(['test']);
  });

  it('a picked candidate shows its own summary', async () => {
    const recentRows = [{ key: 'recent_activity', label: 'Recent activity', status: 'concern' as const, text: 'No setting met the recent window minimum.', link: { kind: 'step' as const, target: 'search' } }];
    const study = studyDetail('awaiting_candidate');
    await renderStep({ ...study, decision_summaries: [decisionSummary('all_period'), decisionSummary('recent', recentRows), decisionSummary('incumbent')] });

    fireEvent.click(screen.getByRole('button', { name: /recent fit/i }));
    const summary = screen.getByRole('region', { name: 'What the research supports' });
    expect(summary.textContent).toContain('No setting met the recent window minimum.');
    expect(summary.textContent).not.toContain('Development activity');
  });

  it('picking a candidate changes the selection, its guidance, and what Review final-test lock sends', async () => {
    const { commands } = await renderStep();

    fireEvent.click(screen.getByRole('button', { name: /recent fit/i }));

    expect(candidateCard(/recent fit/i).className).toContain('selected');
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

    const folded = cards();
    expect(folded).toHaveLength(2);
    expect(folded[1].textContent).toContain('Current settings');
    expect(folded[1].textContent).toContain('same settings as All-period fit');
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

  it("names a frequency plan's development floor from its receipt, not a fixed sample floor", async () => {
    await renderStep(studyDetail('awaiting_candidate', { protocol: frequencyProtocol(), activity: tradeActivity() }));

    const aside = screen.getByRole('complementary');
    expect(aside.textContent).toMatch(/Development activity floor\s*101 trades/);
    expect(aside.textContent).not.toContain('Sample floor');
  });

  it('shows what choosing the candidate implies beside the frozen rules', async () => {
    await renderStep();

    const aside = screen.getByRole('complementary');
    expect(aside.textContent).toMatch(/Your loss ceiling\s*20%/);
    expect(aside.textContent).toMatch(/Sample floor\s*30 trades/);
    expect(aside.textContent).toMatch(/Final test\s*Still locked/);
    expect(aside.textContent).toMatch(/Luck adjustment\s*Not estimable/);
  });

  it('reads every candidate’s detail run once and draws their equity and fall from peak, with a table alternative', async () => {
    const { view, service, charts } = await renderStep();

    const chart = await screen.findByRole('img', {
      name: /development cumulative return and fall from peak\. ends at all-period fit \+8\.7%, recent fit \+11\.6%, current settings \+5\.2%/i,
    });
    expect(chart).not.toBeNull();
    expect(service.candidate.mock.calls.map(([, key]) => key)).toEqual(['all_period', 'recent', 'incumbent']);
    const panel = screen.getByRole('region', { name: 'Equity and fall from peak' });
    expect(panel.textContent).toContain('not the walk-forward test returns');

    fireEvent.click(within(panel).getByRole('button', { name: 'Show as table' }));
    const table = within(panel).getByRole('table', { name: /fall from peak at each session close/i });
    expect(within(table).getAllByRole('row')[2].textContent).toMatch(/2024-06-03\s*\+4\.4%\s*-2\.2%\s*\+5\.8%\s*-2\.9%\s*\+2\.6%\s*-1\.3%/);

    fireEvent.click(evidenceTab('By month'));
    fireEvent.click(evidenceTab('Trades'));
    expect(service.candidate).toHaveBeenCalledTimes(3);
    // Every candidate card draws its own return line, the same runs again.
    expect(within(region('Candidate cards')).getAllByRole('img').map((img) => img.getAttribute('aria-label'))).toEqual([
      'All-period fit development cumulative return. Ends at +8.7%.',
      'Recent fit development cumulative return. Ends at +11.6%.',
      'Current settings development cumulative return. Ends at +5.2%.',
    ]);

    // A study poll brings a new study object with the same runs and evidence: nothing is read or redrawn again.
    const live = () => charts.filter((chart) => !chart.disposed);
    await waitFor(() => expect(live()).toHaveLength(7));
    view.fixture.componentRef.setInput('study', { ...studyDetail('awaiting_candidate') });
    await view.fixture.whenStable();
    expect(service.candidate).toHaveBeenCalledTimes(3);
    expect(live().map((chart) => chart.options.length)).toEqual([1, 1, 1, 1, 1, 1, 1]);
  });

  it('About this chart opens the guide beside the chart, at that chart’s section', async () => {
    const { view } = await renderStep();
    const drawer = view.fixture.debugElement.injector.get(MarkdownDrawerService);

    fireEvent.click(within(screen.getByRole('region', { name: 'Equity and fall from peak' })).getByRole('button', { name: /about this chart/i }));

    expect(drawer.activeDocId()).toBe('golden-search-guide');
    expect(drawer.anchor()).toBe('equity-and-fall');
    expect(drawer.visible()).toBe(true);
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

  it('By month shows how much of the selected result rests on its best trades or month, each value in a table, and says why when unmeasured', async () => {
    await renderStep();

    fireEvent.click(evidenceTab('By month'));
    const curve = await screen.findByRole('region', { name: 'Profit concentration curve' });
    expect((await within(curve).findByRole('img')).getAttribute('aria-label')).toContain('The best 1 trade of 2 make 160.9% of its development net profit.');
    fireEvent.click(within(curve).getByRole('button', { name: 'Show as table' }));
    expect(within(curve).getAllByRole('row').map((row) => row.textContent ?? '')[2]).toMatch(/1\s*50\.0%\s*\+\$243\s*160\.9%/);

    const without = region('Without its best');
    expect(within(without).getByRole('img').getAttribute('aria-label')).toBe(
      'All-period fit without its best. All-period fit: All trades +$8,700; Without its best month +$6,800; Without its best 8 trades +$5,400.',
    );
    fireEvent.click(within(without).getByRole('button', { name: 'Show as table' }));
    const results = within(without).getAllByRole('row').slice(1).map((row) => row.textContent ?? '');
    expect(results[1]).toMatch(/Without its best month \(month from 2025-11-01, ET\)\s*\+\$6,800\s*\+\$1,900/);
    expect(results[2]).toMatch(/Without its best 8 trades\s*\+\$5,400\s*\+\$3,300/);

    // Evidence recorded before concentration was measured says so; it never draws an empty or zero chart.
    fireEvent.click(screen.getByRole('button', { name: /current settings frozen incumbent/i }));
    expect(region('Without its best').textContent).toContain('Not measured for this study');
    expect(within(region('Without its best')).queryByRole('img')).toBeNull();
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

  it('a detail read that fails says so in the equity panel and the tabs, and one retry restores both', async () => {
    const service = fakeService();
    service.candidate.mockRejectedValueOnce(new Error('down'));
    await renderStep(studyDetail('awaiting_candidate'), service);

    const equity = screen.getByRole('region', { name: 'Equity and fall from peak' });
    expect((await within(equity).findByRole('alert')).textContent).toContain('could not be loaded');
    fireEvent.click(evidenceTab('Trades'));
    const alert = within(screen.getByRole('region', { name: 'Candidate evidence' })).getByRole('alert');
    expect(alert.textContent).toContain('could not be loaded');
    fireEvent.click(within(alert).getByRole('button', { name: 'Try again' }));

    expect(await screen.findByRole('table', { name: /development trades of all-period fit/i })).not.toBeNull();
    expect(within(equity).queryByRole('alert')).toBeNull();
  });

  it('lines the candidates up side by side, and charts the selected one’s neighbors and cost stresses with every value in a table', async () => {
    await renderStep();

    const side = region('Candidates side by side');
    expect(within(side).getByRole('img').getAttribute('aria-label')).toContain('for All-period fit, Recent fit, Current settings, each measure on its own scale');
    fireEvent.click(within(side).getByRole('button', { name: 'Show as table' }));
    const measures = within(side).getAllByRole('row').map((row) => row.textContent ?? '');
    expect(measures.find((row) => row.startsWith('Trades a year'))).toMatch(/73\.0\s*45\.5\s*79\.0/);
    expect(measures.find((row) => row.startsWith('Stress runs in profit'))).toMatch(/1 of 1\s*—\s*—/);

    const tornado = region('Neighbor tornado');
    expect(within(tornado).getByRole('img').getAttribute('aria-label')).toContain('All-period fit with its one knob moved one step either side; no tested step loses money.');
    fireEvent.click(within(tornado).getByRole('button', { name: 'Show as table' }));
    const steps = within(tornado).getAllByRole('row').slice(1).map((row) => row.textContent ?? '');
    expect(steps[0]).toMatch(/Fast EMA length\s*One below\s*7\s*Tested\s*\+5\.2%\s*-3\.5%/);
    expect(steps[1]).toMatch(/Candidate\s*8\s*This candidate\s*\+8\.7%\s*—/);
    expect(steps[2]).toMatch(/One above\s*9\s*Invalid — untested: outside the legal domain\s*—\s*—/);

    const stress = region('Cost stress ladder');
    fireEvent.click(within(stress).getByRole('button', { name: 'Show as table' }));
    const rungs = within(stress).getAllByRole('row').slice(1).map((row) => row.textContent ?? '');
    expect(rungs).toHaveLength(2);
    expect(rungs[0]).toMatch(/Base costs\s*\+8\.7%\s*—/);
    expect(rungs[1]).toMatch(/Extra 1¢\/share slippage\s*\+4\.1%\s*-4\.6%/);
  });

  it('a candidate without a neighbor audit or stress runs says so instead of drawing an empty chart', async () => {
    await renderStep();

    fireEvent.click(screen.getByRole('button', { name: /current settings frozen incumbent/i }));

    expect(region('Neighbor tornado').textContent).toContain('No neighbor audit was recorded for Current settings.');
    expect(region('Cost stress ladder').textContent).toContain('No stress run was recorded for Current settings.');
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

  it('passes axe with every chart drawn and with their tables open', async () => {
    const { view } = await renderStep();
    const check = async (): Promise<string[]> => {
      const results = await axe.run(view.container, { rules: { 'color-contrast': { enabled: false } } });
      return results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`);
    };

    await screen.findByRole('img', { name: /development cumulative return and fall from peak/i });
    expect(await check()).toEqual([]);
    for (const toggle of screen.getAllByRole('button', { name: 'Show as table' })) fireEvent.click(toggle);
    expect(await check()).toEqual([]);

    fireEvent.click(evidenceTab('By month'));
    await within(await screen.findByRole('region', { name: 'Profit concentration curve' })).findByRole('img');
    for (const name of ['Profit concentration curve', 'Without its best']) fireEvent.click(within(region(name)).getByRole('button', { name: 'Show as table' }));
    expect(await check()).toEqual([]);
  });
});
