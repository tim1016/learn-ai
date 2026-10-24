import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { render, screen, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { environment } from '../../../environments/environment';
import { StoredStudyPageComponent } from './stored-study-page.component';
import type { StoredStudyRecord, StoredStudyType } from './stored-study';
import { PARENT_ID, RECORD_ID, storedRecord } from './testing/stored-study-fixtures';

// Drawing the charts is Chart.js's job and jsdom has no canvas: what is under test is the page around them.
vi.mock('chart.js', () => ({
  Chart: Object.assign(
    vi.fn(function () {
      return { destroy: vi.fn() };
    }),
    { register: vi.fn() },
  ),
  registerables: [],
}));

const URL = (study: StoredStudyType) => `${environment.pythonServiceUrl}/api/research/study-records/${study}/${RECORD_ID}`;

async function openRun(study: StoredStudyType, answer: StoredStudyRecord | { refuse: number; body?: unknown }) {
  const view = await render(StoredStudyPageComponent, {
    inputs: { study, id: RECORD_ID },
    providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
  });
  const http = TestBed.inject(HttpTestingController);
  const request = http.expectOne(URL(study));
  expect(request.request.method).toBe('GET');
  if ('refuse' in answer) request.flush(answer.body ?? {}, { status: answer.refuse, statusText: 'refused' });
  else request.flush(answer);
  await view.fixture.whenStable();
  view.fixture.detectChanges();
  return { ...view, http };
}

const facts = () => within(screen.getByTestId('stored-facts'));

describe('StoredStudyPageComponent', () => {
  it('shows what a Feature run was asked and what it found, in the Feature study’s own result view', async () => {
    const { http, container } = await openRun('feature-study', storedRecord('feature-study'));

    expect(screen.getByRole('heading', { level: 1, name: 'Feature study, saved run' })).toBeTruthy();
    expect(screen.getByText('Does rsi_14 predict the next move of SPY?')).toBeTruthy();
    expect(facts().getByText('Completed')).toBeTruthy();
    expect(facts().getByText('SPY')).toBeTruthy();
    expect(facts().getByText('5 minute')).toBeTruthy();
    expect(facts().getByText('rsi_14')).toBeTruthy();
    expect(facts().getByText(RECORD_ID)).toBeTruthy();
    expect(container.querySelector('app-feature-report')).toBeTruthy();
    http.verify();
  });

  it.each([
    ['feature-study', 'app-feature-report'],
    ['signal-study', 'app-signal-report'],
    ['cross-sectional-study', 'app-cross-sectional-report'],
    ['indicator-reliability', 'app-indicator-reliability-report'],
    ['realized-vs-iv', 'app-realized-vs-iv-report'],
  ] as const)('draws a saved %s run with %s and with nothing it had to compute', async (study, view) => {
    const { container, http } = await openRun(study, storedRecord(study));

    expect(container.querySelector(view)).toBeTruthy();
    expect(container.querySelectorAll('app-feature-report, app-signal-report, app-cross-sectional-report, app-indicator-reliability-report, app-realized-vs-iv-report')).toHaveLength(1);
    http.verify(); // the one GET for the record was the only request: a stored run is read, never recomputed
  });

  it('opens a saved run again from the record alone, with the same facts and the same result', async () => {
    const first = await openRun('cross-sectional-study', storedRecord('cross-sectional-study'));
    const firstText = first.container.textContent;
    first.fixture.destroy();
    TestBed.resetTestingModule(); // a reload: a new page, a new service, nothing carried over

    const second = await openRun('cross-sectional-study', storedRecord('cross-sectional-study'));

    expect(second.container.textContent).toBe(firstText);
    second.http.verify();
  });

  it('offers Rerun as a link to the study with the run’s id and starts nothing itself', async () => {
    const { http } = await openRun('signal-study', storedRecord('signal-study'));

    const rerun = screen.getByRole('link', { name: 'Rerun with these inputs' });

    expect(rerun.getAttribute('href')).toBe(`/research/signal?rerun=${RECORD_ID}`);
    http.verify();
  });

  it('says a universe run finished partial, why, and still shows the members it did evaluate', async () => {
    await openRun(
      'cross-sectional-study',
      storedRecord('cross-sectional-study', { status: 'partial', status_detail: '2 of 3 members evaluated: 1 errored' }),
    );

    const state = within(document.querySelector('app-workspace-state-block') as HTMLElement);
    expect(state.getByText('This run finished with part of its work missing.')).toBeTruthy();
    expect(state.getByText('2 of 3 members evaluated: 1 errored')).toBeTruthy();
    expect(screen.getByText('Option history request timed out.')).toBeTruthy();
    expect(facts().getByText('SPY, QQQ, AAPL')).toBeTruthy();
  });

  it.each([
    ['failed', 'This run failed.', 'The provider returned no bars.'],
    ['cancelled', 'This run was cancelled before it finished.', 'Cancelled by the user.'],
  ] as const)('shows a %s run with its reason and no result, and still offers to rerun it', async (status, happened, why) => {
    const { container } = await openRun(
      'feature-study',
      storedRecord('feature-study', { status, status_detail: why, result: null }),
    );

    const state = within(document.querySelector('app-workspace-state-block') as HTMLElement);
    expect(state.getByText(happened)).toBeTruthy();
    expect(state.getByText(why)).toBeTruthy();
    expect(container.querySelector('app-feature-report')).toBeNull();
    expect(screen.getByRole('link', { name: 'Rerun with these inputs' })).toBeTruthy();
  });

  it('links a rerun to the run it was prefilled from', async () => {
    await openRun(
      'feature-study',
      storedRecord('feature-study', { rerun_of: { type: 'feature-study', native_id: PARENT_ID } }),
    );

    const link = within(screen.getByTestId('stored-facts')).getByRole('link', { name: PARENT_ID.slice(0, 8) });
    expect(link.getAttribute('href')).toBe(`/research/history/feature-study/${PARENT_ID}`);
  });

  it('reads “not recorded” for the data source of a run whose result carried none, never today’s provider', async () => {
    await openRun('feature-study', storedRecord('feature-study'));

    const line = document.querySelector('app-workspace-data-source') as HTMLElement;
    expect(line.textContent).toContain('not recorded');
    expect(line.textContent).not.toMatch(/polygon/i);
  });

  it('fails in place with a way back to the History when no run is saved under the id', async () => {
    const { container } = await openRun('signal-study', { refuse: 404, body: { detail: { code: 'STUDY_RECORD_NOT_FOUND' } } });

    expect(screen.getByText('This run is not saved.')).toBeTruthy();
    expect(screen.getByText('No Signal study run is saved under this id.')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Back to History' }).getAttribute('href')).toBe('/research/history?type=signal-study');
    expect(screen.queryByRole('link', { name: 'Rerun with these inputs' })).toBeNull();
    expect(container.querySelector('app-signal-report')).toBeNull();
  });

  it('says a record the data plane cannot read is unreadable, with its reason, and offers no result', async () => {
    await openRun('feature-study', {
      refuse: 500,
      body: { detail: { code: 'STUDY_RECORD_UNREADABLE', message: 'It may have been written by a newer build.' } },
    });

    expect(screen.getByText('This saved run could not be read.')).toBeTruthy();
    expect(screen.getByText('It may have been written by a newer build.')).toBeTruthy();
  });

  it('offers to try again when the data plane did not answer, and reads the record again on request', async () => {
    const { fixture, http } = await openRun('feature-study', { refuse: 503 });
    expect(screen.getByText('This saved run could not be opened.')).toBeTruthy();

    screen.getByRole('button', { name: 'Try again' }).click();
    fixture.detectChanges();

    http.expectOne(URL('feature-study')).flush(storedRecord('feature-study'));
    await fixture.whenStable();
    fixture.detectChanges();
    expect(screen.getByText('Does rsi_14 predict the next move of SPY?')).toBeTruthy();
  });

  it('keeps the run and says so when its result cannot be drawn', async () => {
    await openRun('realized-vs-iv', storedRecord('realized-vs-iv', { result: { ...storedRecord('realized-vs-iv').result, bars: [] } as never }));

    expect(screen.getByText('The saved result could not be shown.')).toBeTruthy();
    expect(
      screen.getByText('this run was saved in a form from before the study read the data lake, which this page can no longer draw'),
    ).toBeTruthy();
    expect(facts().getByText('SPY')).toBeTruthy();
  });
});
