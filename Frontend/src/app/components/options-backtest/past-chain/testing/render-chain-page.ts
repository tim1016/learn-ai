import type { ComponentFixture } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { render, screen } from '@testing-library/angular';
import type { vi as Vi } from 'vitest';

import { JobsService } from '../../../../services/jobs.service';
import { fakePickerWorld } from '../../../../shared/symbol-picker/testing/fake-picker-world';
import { SymbolPickerComponent } from '../../../../shared/symbol-picker/symbol-picker.component';
import { OptionsBacktestPageComponent } from '../../options-backtest-page.component';
import { OptionsBacktestService } from '../../options-backtest.service';
import { RUN_POLL_MS } from '../../run-poll';
import { fakeJobs } from '../../testing/fake-jobs';
import { fakeService, type FakeService } from '../../testing/render-page';
import { PastChainService } from '../past-chain.service';
import type { FakeChainService } from './render-chain';

type Spy = Pick<typeof Vi, 'fn'>;

/**
 * The whole page, with the Past chain's service replaced as well. The page's own helper cannot take another provider, so
 * the same providers are put together here; nothing of the page's helpers is changed.
 */
export async function renderPageWithChain(spy: Spy, chain: FakeChainService, service: FakeService = fakeService(spy)) {
  const view = await render(OptionsBacktestPageComponent, {
    inputs: { describeDebounceMs: 0 },
    providers: [
      ...fakePickerWorld().providers,
      { provide: RUN_POLL_MS, useValue: 0 },
      { provide: OptionsBacktestService, useValue: service },
      { provide: JobsService, useValue: fakeJobs().service },
      { provide: PastChainService, useValue: chain },
    ],
  });
  await screen.findByRole('heading', { name: 'Entry settings' });
  await view.fixture.whenStable();
  return view;
}

/** The symbol pickers of the page in the order they stand: the Instrument card's first, the Past chain's after it once that tab is made. */
export function symbolPickers(fixture: ComponentFixture<unknown>): SymbolPickerComponent[] {
  return fixture.debugElement.queryAll(By.directive(SymbolPickerComponent)).map((each) => each.componentInstance as SymbolPickerComponent);
}
