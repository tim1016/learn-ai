import { provideRouter } from '@angular/router';
import { render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';

import receipt from '../../../../../PythonDataService/tests/fixtures/options_evidence/lineage-read.json';
import trials from '../../../../../PythonDataService/tests/fixtures/options_evidence/lineage-trials.json';
import { OptionsResearchLineageComponent } from './options-research-lineage.component';
import { OptionsResearchLineageService, type ResearchLineage } from './options-research-lineage.service';

it('explains six no-winner folds and loads durable trials only on request', async () => {
  const ledger = vi.fn(async () => trials);
  await render(OptionsResearchLineageComponent, { inputs: { studyId: receipt.study_id }, providers: [provideRouter([]),
    { provide: OptionsResearchLineageService, useValue: { read: async () => receipt, trials: ledger } }] });
  expect(await screen.findByText('27 / 27')).toBeTruthy();
  expect(screen.getAllByText('No eligible training winner')).toHaveLength(6);
  expect(screen.getByText('The final test was not opened. No final outcomes are exposed by this view.')).toBeTruthy();
  expect(ledger).not.toHaveBeenCalled();
  await userEvent.click(screen.getByText('Inspect retained trial ledger', { exact: true }));
  await waitFor(() => expect(ledger).toHaveBeenCalledWith(receipt.study_id, '0'));
  expect(await screen.findAllByText('Cache Hit')).toHaveLength(7);
});

it('rejects stale lineage responses after another exact study is selected', async () => {
  let finish: ((value: ResearchLineage) => void) | undefined;
  const slow = new Promise<ResearchLineage>(resolve => { finish = resolve; });
  const read = vi.fn((id: string) => id === 'old' ? slow : Promise.resolve(receipt));
  const view = await render(OptionsResearchLineageComponent, { inputs: { studyId: 'old' }, providers: [provideRouter([]),
    { provide: OptionsResearchLineageService, useValue: { read } }] });
  await waitFor(() => expect(read).toHaveBeenCalledWith('old'));
  view.fixture.componentRef.setInput('studyId', receipt.study_id);
  view.fixture.detectChanges();
  expect(await screen.findAllByText('No eligible training winner')).toHaveLength(6);
  finish?.({ ...receipt, study_id: 'old', objective: 'Prior subject must not replace selection' } as ResearchLineage);
  await view.fixture.whenStable();
  expect(screen.queryByText('Prior subject must not replace selection')).toBeNull();
});
