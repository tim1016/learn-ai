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
    expect(second.textContent).toContain('No Eligible Candidate');
    expect(second.textContent).toContain('No winner');
    expect(second.textContent).toContain('No result recorded.');
  });

  it('reads a key the canonical point omits as its declared default, so an explicit default is no change', async () => {
    const base = validationView();
    // The frozen seed omits fast_period (identity-neutral default 5); this winner states it explicitly.
    const winner = { ...INCUMBENT_PARAMS, fast_period: 5 };
    await renderStep(withValidation({ ...base, folds: [{ ...base.folds[0], winner }] }));

    expect(foldRow(1).textContent).toContain('Same as the starting point');
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
    expect(svg?.querySelectorAll('path')).toHaveLength(0);
    expect(svg?.querySelectorAll('circle')).toHaveLength(1);
  });

  it('draws one unbroken segment when every fold has a linked value', async () => {
    const base = validationView();
    const view = await renderStep(withValidation({ ...base, linked: [{ ...base.linked[0] }, { ...base.linked[1], linked_return: -0.013 }] }));

    expect(view.container.querySelectorAll('app-golden-search-linked-line path')).toHaveLength(1);
  });

  it('before the procedure is tested over time, says what will happen instead of showing empty results', async () => {
    await renderStep(studyDetail('awaiting_validation'));

    expect(screen.getByRole('status').textContent).toContain('has not been tested over time yet');
    expect(screen.queryByRole('table')).toBeNull();
  });
});
