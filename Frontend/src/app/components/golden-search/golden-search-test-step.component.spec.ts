import { render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { GoldenSearchTestStepComponent } from './golden-search-test-step.component';
import type { StudyDetail, ValidationView } from './golden-search.types';
import { emaCapability, INCUMBENT_PARAMS, studyDetail, validationView } from './testing/fixtures';

async function renderStep(study: StudyDetail) {
  return render(GoldenSearchTestStepComponent, { inputs: { study, capability: emaCapability() } });
}

function withValidation(validation: ValidationView): StudyDetail {
  const study = studyDetail('awaiting_candidate');
  return { ...study, results: { ...study.results, validation } };
}

function foldRow(index: number): HTMLElement {
  const table = screen.getByRole('table', { name: /training winner and its test result/i });
  const row = within(table).getByRole('rowheader', { name: String(index) }).closest('tr');
  if (row === null) throw new Error(`no row for fold ${index}`);
  return row;
}

describe('GoldenSearchTestStepComponent', () => {
  it('lists every fold, a failed one included, with the winner’s change from the starting point and both results', async () => {
    await renderStep(studyDetail('awaiting_candidate'));

    const first = foldRow(1);
    expect(first.textContent).toContain('Fast EMA length 8');
    expect(first.textContent).not.toContain('Crossover gap');
    expect(first.textContent).toMatch(/Sharpe\s*1\.40/);
    expect(first.textContent).toMatch(/Sharpe\s*0\.90/);

    const second = foldRow(2);
    expect(second.textContent).toContain('Failed');
    expect(second.textContent).toContain("No setting met your rules in this fold's training window.");
    expect(second.textContent).toContain('No winner');
    expect(second.textContent).toContain('No result recorded.');
  });

  it('reads every fold against the frozen incumbent on the same test months, a failed fold included', async () => {
    await renderStep(studyDetail('awaiting_candidate'));

    const benchmark = (index: number): string => foldRow(index).querySelectorAll('td')[6]?.textContent ?? '';
    expect(benchmark(1)).toMatch(/Trades\s*17/);
    expect(benchmark(1)).toMatch(/Sharpe\s*0\.41/);
    expect(benchmark(2)).toMatch(/Sharpe\s*-0\.20/);

    const linked = within(screen.getByRole('table', { name: /linked test-period return by fold/i })).getAllByRole('row');
    expect(linked[1].textContent).toMatch(/2\.10%\s*0\.70%/);
    expect(linked[2].textContent).toMatch(/Line broken — fold missing\s*0\.30%/);
  });

  it('reads a key the canonical point omits as its declared default, so an explicit default is no change', async () => {
    const base = validationView();
    // The frozen seed omits fast_period (identity-neutral default 5); this winner states it explicitly.
    const winner = { ...INCUMBENT_PARAMS, fast_period: 5 };
    await renderStep(withValidation({ ...base, folds: [{ ...base.folds[0], winner }] }));

    expect(foldRow(1).textContent).toContain('Same as the starting point');
  });

  it('a test over time cut short by the budget says some folds are missing; a complete one says nothing', async () => {
    const view = await renderStep(withValidation(validationView({ incomplete: true })));
    const note = /testing over time stopped at its evaluation budget, so some folds are missing/i;

    expect(screen.getByText(note)).not.toBeNull();
    view.fixture.componentRef.setInput('study', withValidation(validationView()));
    await view.fixture.whenStable();
    expect(screen.queryByText(note)).toBeNull();
  });

  it('shows the legacy verdict with the folds it is based on', async () => {
    await renderStep(studyDetail('awaiting_candidate'));

    const verdict = screen.getByRole('region', { name: /could not be judged/i });
    expect(verdict.textContent).toContain('based on 1 of 2 folds');
    expect(verdict.textContent).toMatch(/Median fold retention\s*—/);
  });

  it('breaks the linked line at a missing fold instead of joining across it, with the table as its alternative', async () => {
    const view = await renderStep(studyDetail('awaiting_candidate'));

    const table = screen.getByRole('table', { name: /linked test-period return by fold/i });
    expect(within(table).getAllByRole('row')[1].textContent).toContain('2.10%');
    expect(within(table).getAllByRole('row')[2].textContent).toContain('Line broken — fold missing');
    const svg = view.container.querySelector('app-golden-search-linked-line svg');
    expect(svg?.getAttribute('aria-hidden')).toBe('true');
    expect(svg?.querySelectorAll('path.line')).toHaveLength(0);
    expect(svg?.querySelectorAll('circle')).toHaveLength(1);
    expect(svg?.querySelectorAll('path.benchmark')).toHaveLength(1);
  });

  it('draws one unbroken segment when every fold has a linked value', async () => {
    const base = validationView();
    const view = await renderStep(withValidation({ ...base, linked: [{ ...base.linked[0] }, { ...base.linked[1], linked_return: -0.013 }] }));

    expect(view.container.querySelectorAll('app-golden-search-linked-line path.line')).toHaveLength(1);
  });

  it('summarizes the folds judged, the test trades and the median retention as the server worded them', async () => {
    await renderStep(withValidation(validationView({ summary_pills: { judged: '5 of 6 folds judged', test_trades: 142, median_retention: 0.61 } })));

    const pills = screen.getByRole('list', { name: 'Test-over-time summary' });
    expect(within(pills).getAllByRole('listitem').map((item) => item.textContent?.trim())).toEqual(['5 of 6 folds judged', '142 test trades', '0.61 median retention']);
    expect(screen.getByText(/not a probability of future profit/i)).not.toBeNull();
  });

  it('before the procedure is tested over time, says what will happen instead of showing empty results', async () => {
    await renderStep(studyDetail('awaiting_validation'));

    expect(screen.getByRole('status').textContent).toContain('has not been tested over time yet');
    expect(screen.queryByRole('table')).toBeNull();
  });
});
