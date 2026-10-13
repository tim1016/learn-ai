import type { Provider } from '@angular/core';
import { render } from '@testing-library/angular';
import type { vi as Vi } from 'vitest';

import { JobsService } from '../../../services/jobs.service';
import { fakePickerWorld } from '../../../shared/symbol-picker/testing/fake-picker-world';
import { HistoryTabComponent } from '../history/history-tab.component';
import { OptionsBacktestService } from '../options-backtest.service';
import { RUN_POLL_MS } from '../run-poll';
import { RunDetailService } from '../run-detail/run-detail.service';
import { inertRunDetail, type FakeRunDetail } from '../run-detail/testing/fake-run-detail';
import { fakeJobs, type FakeJobs } from './fake-jobs';
import { fakeService, type FakeService } from './render-page';

type Spy = Pick<typeof Vi, 'fn'>;

/**
 * The History tab on its own. The poll that reads the list (and a run opened from it) again while a run is going is off
 * unless `polling` is set; it then runs at its own default, which a spec that is about the poll drives with fake timers.
 */
export async function renderHistoryTab(
  spy: Spy,
  options: { service?: FakeService; jobs?: FakeJobs; detail?: FakeRunDetail; active?: boolean; polling?: boolean; providers?: readonly Provider[] } = {},
) {
  const service = options.service ?? fakeService(spy);
  const jobs = options.jobs ?? fakeJobs();
  const detail = options.detail ?? inertRunDetail();
  const view = await render(HistoryTabComponent, {
    inputs: { active: options.active ?? true },
    providers: [
      ...fakePickerWorld().providers,
      { provide: OptionsBacktestService, useValue: service },
      { provide: JobsService, useValue: jobs.service },
      { provide: RunDetailService, useValue: detail },
      ...(options.polling === true ? [] : [{ provide: RUN_POLL_MS, useValue: 0 }]),
      ...(options.providers ?? []),
    ],
  });
  return { view, service, jobs };
}
