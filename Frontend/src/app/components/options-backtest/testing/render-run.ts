import { render } from '@testing-library/angular';
import type { vi as Vi } from 'vitest';

import { JobsService } from '../../../services/jobs.service';
import { OptionsBacktestService } from '../options-backtest.service';
import type { RunDetail, RunListItem } from '../options-backtest.types';
import { RunPanelComponent } from '../run/run-panel.component';
import { RUN_POLL_MS } from '../run-poll';
import { fakeJobs, type FakeJobs } from './fake-jobs';
import { fakeService, type FakeService } from './render-page';
import { runDetail } from './run-fixtures';

type Spy = Pick<typeof Vi, 'fn'>;

export interface RenderedRun {
  readonly view: Awaited<ReturnType<typeof render<RunPanelComponent>>>;
  readonly service: FakeService;
  readonly jobs: FakeJobs;
  /** What the panel told the page: each record it read that said something other than it was given. */
  readonly updated: RunDetail[];
}

/**
 * One run on screen, as either tab shows it. The poll that reads a going run again is off unless `polling` is set; it then
 * runs at its own default, which a spec that is about the poll drives with fake timers.
 */
export async function renderRunPanel(
  spy: Spy,
  run: RunListItem,
  options: { service?: FakeService; jobs?: FakeJobs; showSetup?: boolean; polling?: boolean } = {},
): Promise<RenderedRun> {
  const service = options.service ?? fakeService(spy);
  const jobs = options.jobs ?? fakeJobs();
  const view = await render(RunPanelComponent, {
    inputs: { run, showSetup: options.showSetup ?? false },
    providers: [
      { provide: OptionsBacktestService, useValue: service },
      { provide: JobsService, useValue: jobs.service },
      ...(options.polling === true ? [] : [{ provide: RUN_POLL_MS, useValue: 0 }]),
    ],
  });
  const updated: RunDetail[] = [];
  view.fixture.componentInstance.updated.subscribe((detail) => updated.push(detail));
  return { view, service, jobs, updated };
}

/** The record the run is read as, once for each status it passes through (the last one for every read after). */
export function readsAs(service: FakeService, ...records: RunDetail[]): void {
  let next = 0;
  service.run.mockImplementation(async () => records[Math.min(next++, records.length - 1)] ?? runDetail());
}
