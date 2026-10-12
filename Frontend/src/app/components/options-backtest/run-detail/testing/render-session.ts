import { render } from '@testing-library/angular';
import type { vi as Vi } from 'vitest';

import type { RunDetail } from '../../options-backtest.types';
import { runDetail } from '../../testing/run-fixtures';
import type { DayRequest } from '../day-request';
import { RunDetailService } from '../run-detail.service';
import { SessionViewComponent } from '../session/session-view.component';
import { TIMELINE_CHART_FACTORY, TIMELINE_COLOURS } from '../session/timeline-chart.component';
import { fakeRunDetail, type FakeRunDetail } from './fake-run-detail';
import { fakeTimelineChart, type FakeTimelineChart } from './timeline-chart-fake';

type Spy = Pick<typeof Vi, 'fn'>;

export interface RenderedSession {
  readonly view: Awaited<ReturnType<typeof render<SessionViewComponent>>>;
  readonly detail: FakeRunDetail;
  readonly chart: FakeTimelineChart;
}

/**
 * One day's view on its own, with the chart library replaced: the chart it makes is the fake, and the colours it reads are
 * the names of the tokens, so a spec sees what was handed to the library. A spec that renders this must mock
 * `lightweight-charts` itself (`vi.mock` is hoisted per file).
 */
export async function renderSession(
  spy: Spy,
  options: { detail?: FakeRunDetail; run?: RunDetail; request?: DayRequest | null; chart?: FakeTimelineChart } = {},
): Promise<RenderedSession> {
  const detail = options.detail ?? fakeRunDetail(spy);
  const chart = options.chart ?? fakeTimelineChart(spy);
  const view = await render(SessionViewComponent, {
    inputs: { run: options.run ?? runDetail(), request: options.request ?? null },
    providers: [
      { provide: RunDetailService, useValue: detail },
      { provide: TIMELINE_CHART_FACTORY, useValue: chart.factory },
      { provide: TIMELINE_COLOURS, useValue: (_host: HTMLElement, cssVar: string) => `colour(${cssVar})` },
    ],
  });
  return { view, detail, chart };
}
