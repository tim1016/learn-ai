import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { GoldenSearchTestStepComponent } from './golden-search-test-step.component';
import { GoldenSearchService } from './golden-search.service';
import type { StudyDetail, TestOverTimeCharts } from './golden-search.types';
import { fakeCharts } from './testing/fake-charts';
import { plannedTestOverTimeCharts, studyDetail, testOverTimeCharts, validationView } from './testing/fixtures';

async function renderStep(study: StudyDetail, charts: () => Promise<TestOverTimeCharts> = async () => testOverTimeCharts()) {
  const library = fakeCharts();
  const service = { testOverTimeCharts: vi.fn(charts) };
  const view = await render(GoldenSearchTestStepComponent, {
    inputs: { study },
    providers: [{ provide: GoldenSearchService, useValue: service }, ...library.providers],
  });
  return { view, service, charts: library.charts };
}

function region(name: string): HTMLElement {
  return screen.getByRole('region', { name });
}

function tableRows(panel: string): string[] {
  fireEvent.click(within(region(panel)).getByRole('button', { name: 'Show as table' }));
  return within(region(panel)).getAllByRole('row').map((row) => row.textContent ?? '');
}

describe('GoldenSearchTestStepComponent', () => {
  it('charts every fold, a failed one included, with its reason, retention, returns and trades against the forward minimum', async () => {
    await renderStep(studyDetail('awaiting_candidate'));

    const timeline = await screen.findByRole('region', { name: 'Fold timeline' });
    expect(within(timeline).getByRole('img').getAttribute('aria-label')).toContain('2 folds, each a training window followed by a test window; 1 completed.');
    const folds = tableRows('Fold timeline');
    expect(folds[1]).toMatch(/Fold 1.*Completed\s*1\.40\s*0\.90\s*64%\s*\+2\.1%\s*42/);
    expect(folds[2]).toContain("Failed: No setting met your rules in this fold's training window.");

    expect(tableRows('Linked test return')[2]).toMatch(/Fold 2.*fold missing\s*\+0\.3%/);
    expect(tableRows('Test return per fold')[1]).toMatch(/Fold 1\s*\+2\.1%\s*\+0\.7%\s*\+1\.4%/);
    expect(region('Test activity per fold').querySelector('[role=img]')?.getAttribute('aria-label')).toContain('together 42 against a minimum of 30');
    expect(tableRows('Parameter drift')[1]).toMatch(/Fast EMA length\s*8\s*—\s*8\s*5/);
  });

  it('reads the charts once per study revision, not on every poll', async () => {
    const study = studyDetail('awaiting_candidate');
    const { view, service } = await renderStep(study);
    await screen.findByRole('region', { name: 'Fold timeline' });

    view.fixture.componentRef.setInput('study', { ...study });
    await view.fixture.whenStable();
    expect(service.testOverTimeCharts).toHaveBeenCalledTimes(1);
    view.fixture.componentRef.setInput('study', { ...study, revision: study.revision + 1 });
    await waitFor(() => expect(service.testOverTimeCharts).toHaveBeenCalledTimes(2));
  });

  it('while testing over time runs, counts the trades so far without judging them, and holds back the linked return', async () => {
    const running = async () => testOverTimeCharts({ in_progress: true, test_trades_total: 12, forward_minimum: 30, linked: [], incumbent_linked: [] });
    await renderStep(studyDetail('awaiting_candidate'), running);

    const activity = (await screen.findByRole('region', { name: 'Test activity per fold' })).querySelector('[role=img]')?.getAttribute('aria-label') ?? '';
    expect(activity).toContain('so far 12, testing still under way');
    expect(activity).not.toContain('minimum');
    expect(region('Linked test return').textContent).toContain('drawn when testing over time finishes');
  });

  it('before testing over time runs, shows the planned folds and says what will happen', async () => {
    await renderStep(studyDetail('awaiting_validation'), async () => plannedTestOverTimeCharts());

    expect(screen.getByText(/has not been tested over time yet/)).not.toBeNull();
    const timeline = await screen.findByRole('region', { name: 'Fold timeline' });
    expect(within(timeline).getByRole('img').getAttribute('aria-label')).toContain('2 planned folds');
    expect(screen.queryByRole('region', { name: 'Linked test return' })).toBeNull();
  });

  it('keeps the legacy verdict and the cut-short note beside the charts', async () => {
    const study = studyDetail('awaiting_candidate');
    await renderStep({ ...study, results: { ...study.results, validation: validationView({ incomplete: true }) } });

    expect(screen.getByRole('region', { name: /could not be judged/i }).textContent).toContain('based on 1 of 2 folds');
    expect(screen.getByText(/testing over time stopped at its evaluation budget/i)).not.toBeNull();
    expect(screen.getByText(/not a probability of future profit/i)).not.toBeNull();
  });

  it('a chart read that fails says so, and Try again reads it again', async () => {
    const fails = vi.fn().mockRejectedValueOnce(new Error('down')).mockResolvedValue(testOverTimeCharts());
    await renderStep(studyDetail('awaiting_candidate'), fails);

    fireEvent.click(within(await screen.findByRole('alert')).getByRole('button', { name: 'Try again' }));
    expect(await screen.findByRole('region', { name: 'Fold timeline' })).not.toBeNull();
  });

  it('passes axe with every chart drawn and every table open', async () => {
    const { view } = await renderStep(studyDetail('awaiting_candidate'));
    await screen.findByRole('region', { name: 'Fold timeline' });
    for (const toggle of screen.getAllByRole('button', { name: 'Show as table' })) fireEvent.click(toggle);

    const results = await axe.run(view.container, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`)).toEqual([]);
  });
});
