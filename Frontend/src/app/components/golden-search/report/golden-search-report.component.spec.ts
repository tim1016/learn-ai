import { provideRouter } from '@angular/router';
import { fireEvent, render, screen, within } from '@testing-library/angular';
import axe from 'axe-core';
import { expect, it, vi } from 'vitest';
import { GoldenSearchService } from '../golden-search.service';
import type { CandidateKey, CurveSetKey } from '../golden-search.types';
import { fakeCharts } from '../testing/fake-charts';
import { candidateDetail, chanceCarryOver, curveSet, emaCapability, examFit, examView, finalChance, studyDetail, testOverTimeCharts } from '../testing/fixtures';
import { GoldenSearchReportComponent } from './golden-search-report.component';

it('opens the recorded approved fit and keeps its identity when comparing another fit', async () => {
  const base = studyDetail('approved', { candidate_key: 'recent' });
  const exam = examView({ chosen_key: 'recent' });
  const service = { testOverTimeCharts: async () => testOverTimeCharts(), curveSet: async (_id: string, set: CurveSetKey) => curveSet(set), candidate: async (_id: string, key: CandidateKey) => candidateDetail(key), command: vi.fn() };
  await render(GoldenSearchReportComponent, { inputs: { study: { ...base, results: { ...base.results, exam } } }, providers: [provideRouter([]), { provide: GoldenSearchService, useValue: service }, ...fakeCharts().providers] });
  const picker = screen.getByRole('navigation', { name: 'Fit shown in final charts' });
  expect(within(picker).getByRole('button', { name: 'Recent fit' }).getAttribute('aria-pressed')).toBe('true');
  expect(screen.getByRole('list', { name: 'Final-test checks for Recent fit' })).not.toBeNull();
  const conclusion = screen.getByRole('region', { name: 'Current conclusion' });
  expect(conclusion.textContent).toContain('Recorded approved fit: Recent fit');
  fireEvent.click(within(picker).getByRole('button', { name: 'All-period fit' }));
  await screen.findByRole('list', { name: 'Final-test checks for All-period fit' });
  expect(conclusion.textContent).toContain('Recorded approved fit: Recent fit');
  expect(service.command).not.toHaveBeenCalled();
});

it('shows all final fits, the Sharpe rehearsal rank and their failures while chart selection remains read-only', async () => {
  const base = studyDetail('awaiting_review');
  const carry = chanceCarryOver();
  const sharpe = { ...carry, basis: { ...carry.basis, objective: { measure: 'sharpe_ratio' as const, label: 'Sharpe', units: 'ratio' } } };
  const exam = examView({ claim: 'exploratory', exposure_state: 'previously_used', fits: [examFit('all_period', { chance: finalChance({ carry_over: sharpe }) }), examFit('recent', { outcome: 'could_not_evaluate', metrics: null })] });
  const service = { testOverTimeCharts: async () => testOverTimeCharts(), curveSet: async (_id: string, set: CurveSetKey) => curveSet(set), candidate: vi.fn(async (_id: string, key: CandidateKey) => candidateDetail(key)), command: vi.fn() };
  const view = await render(GoldenSearchReportComponent, { inputs: { study: { ...base, results: { ...base.results, exam } }, capability: emaCapability() }, providers: [provideRouter([]), { provide: GoldenSearchService, useValue: service }, ...fakeCharts().providers] });
  expect(screen.getByText(/Exploratory research/)).not.toBeNull();
  expect(screen.queryByText(/different return conventions/)).toBeNull();
  expect(screen.getByRole('region', { name: 'Development rehearsal comparison' }).textContent).toContain('Where the recorded result sits among its own rehearsals');
  const final = screen.getByRole('region', { name: 'What survived the final test?' });
  expect(within(final).getByText('No result recorded')).not.toBeNull();
  fireEvent.click(within(screen.getByRole('navigation', { name: 'Fit shown in final charts' })).getByRole('button', { name: 'Recent fit' }));
  expect(service.command).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', { name: /approve|deploy|open.*final test/i })).toBeNull();
  await screen.findByRole('region', { name: 'Fold timeline' });
  const result = await axe.run(view.container, { rules: { 'color-contrast': { enabled: false } } });
  expect(result.violations.map(item => ({ id: item.id, targets: item.nodes.map(node => node.target) }))).toEqual([]);
});
