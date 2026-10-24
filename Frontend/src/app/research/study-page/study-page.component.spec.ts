import { provideRouter } from '@angular/router';
import { render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { StudyPageComponent } from './study-page.component';
import { RESEARCH_STUDIES, type ResearchStudyId } from '../research-studies';
import type { StudyPrefill } from '../stored-study/rerun';
import type { DataSourceProvenance } from '../../shared/workspace/data-source-provenance';

interface Page {
  readonly study: ResearchStudyId;
  readonly canRun?: boolean;
  readonly running?: boolean;
  readonly blockedReason?: string | null;
  readonly hasOutput?: boolean;
  readonly hasResult?: boolean;
  readonly previous?: boolean;
  readonly prefill?: StudyPrefill | null;
  readonly dataSource?: DataSourceProvenance;
  readonly dataSourceWindows?: 'et' | 'date-et';
}

async function renderPage(page: Page) {
  const run = vi.fn();
  await render(
    `<app-study-page
       [study]="study"
       readySummary="RSI(14) on SPY, 2025-10-01 to 2026-10-01"
       [canRun]="canRun"
       [running]="running"
       [blockedReason]="blockedReason"
       [hasOutput]="hasOutput"
       [hasResult]="hasResult"
       [previous]="previous"
       [captured]="captured"
       [prefill]="prefill"
       [dataSource]="dataSource"
       [dataSourceWindows]="dataSourceWindows"
       (run)="run()">
       <div studyInstrument>instrument picker</div>
       <div studyConfig>test configuration</div>
       <div studyProgress>progress</div>
       <div studyResult>the result</div>
     </app-study-page>`,
    {
      imports: [StudyPageComponent],
      providers: [provideRouter([])],
      componentProperties: {
        canRun: true,
        running: false,
        blockedReason: null,
        hasOutput: false,
        hasResult: false,
        previous: false,
        prefill: null,
        dataSource: undefined,
        dataSourceWindows: 'et',
        captured: { key: 'k', facts: [{ label: 'Instrument', value: 'SPY' }] },
        ...page,
        run,
      },
    },
  );
  return { run, user: userEvent.setup() };
}

describe('StudyPageComponent', () => {
  it('states the question, then the instrument and window, the configuration and Run study, in that order', async () => {
    await renderPage({ study: 'feature' });

    expect(screen.getByRole('heading', { level: 1, name: 'Feature study' })).toBeTruthy();
    expect(screen.getByText('Does this indicator predict the next move for this instrument?')).toBeTruthy();
    const steps = screen.getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent?.trim());
    expect(steps).toEqual(['1Instrument and window', '2Test configuration', '3Run']);
    expect(screen.getByRole('button', { name: 'Run study' })).toBeTruthy();
  });

  it('asks a universe study for its universe, not for one instrument', async () => {
    await renderPage({ study: 'cross-sectional' });

    const [first] = screen.getAllByRole('heading', { level: 2 });
    expect(first.textContent?.trim()).toBe('1Universe and window');
  });

  it.each([
    ['feature', '15 minutes, fixed'],
    ['signal', 'extended hours included, fixed'],
    ['indicator-reliability', '1 minute, fixed'],
    ['cross-sectional', 'daily, fixed'],
  ] as const)('says what %s fixes in place of a control: %s', async (study, fixed) => {
    await renderPage({ study });

    expect(within(screen.getByTestId('study-constraints')).getByText(fixed)).toBeTruthy();
  });

  it.each([
    ['feature', 'Data lake, Polygon'],
    ['signal', 'Polygon'],
    ['indicator-reliability', 'Data lake, Polygon'],
    ['cross-sectional', 'Polygon stock bars, Polygon option bars, FRED risk-free rate'],
    ['realized-vs-iv', 'Polygon aggregates, IV recorder (recorded IV30), Polygon option snapshot (current IV marker)'],
  ] as const)(
    'names the provider behind %s on its data-source line, and reads "not recorded" for what it does not record',
    async (study, sources) => {
      await renderPage({ study });

      expect(screen.getByText('Data source').closest('summary')?.textContent).toContain(sources);
      expect(screen.getAllByText('not recorded').length).toBeGreaterThan(0);
    },
  );

  describe('a lake window handed over as the study\'s data source', () => {
    // 2024-03-04 and 2024-03-15 at ET midnight: the bounds of a window of sessions.
    const lakeSource: DataSourceProvenance = {
      sources: ['Data lake', 'polygon'],
      subject: 'SPY',
      requestedWindow: { startMs: 1_709_528_400_000, endMs: 1_710_478_800_000 },
      effectiveWindow: { startMs: 1_709_528_400_000, endMs: 1_710_478_800_000 },
      warmup: 'none',
      resolution: '5-minute bars, folded from 1-minute bars',
      adjustment: 'polygon_split_adjusted',
      sessionPolicy: 'extended',
      dataIdentity: 'abc',
      observedAtMs: null,
      counts: { added: 3, reused: 7, missing: 0 },
      limitations: [],
    };
    const lineText = () => screen.getByText('Data source').closest('summary')?.textContent ?? '';

    it('replaces the study\'s own line, and writes a window of dates without a time of day', async () => {
      await renderPage({ study: 'feature', dataSource: lakeSource, dataSourceWindows: 'date-et' });

      expect(lineText()).toContain('Data lake, Polygon');
      expect(lineText()).toContain('5-minute bars, folded from 1-minute bars');
      expect(lineText()).not.toMatch(/\d{1,2}:\d{2}/);
    });

    it('writes the exchange-aligned instant unless the host says its windows are dates', async () => {
      await renderPage({ study: 'feature', dataSource: lakeSource });

      expect(lineText()).toMatch(/\d{1,2}:\d{2}/);
    });
  });

  it('has a data-source line for every registered study', () => {
    expect(RESEARCH_STUDIES.map((study) => study.id).sort()).toEqual(
      ['cross-sectional', 'feature', 'indicator-reliability', 'realized-vs-iv', 'signal'],
    );
  });

  it('reports one press of Run study and says what is about to run', async () => {
    const { run, user } = await renderPage({ study: 'feature' });

    await user.click(screen.getByRole('button', { name: 'Run study' }));
    expect(run).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Ready: RSI(14) on SPY, 2025-10-01 to 2026-10-01')).toBeTruthy();
  });

  it('disables Run study and gives the reason while the inputs are incomplete', async () => {
    await renderPage({ study: 'feature', canRun: false, blockedReason: 'Choose an instrument and both dates.' });

    expect((screen.getByRole('button', { name: 'Run study' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('Choose an instrument and both dates.')).toBeTruthy();
  });

  it('disables Run study while a run is in progress', async () => {
    await renderPage({ study: 'feature', running: true });

    expect((screen.getByRole('button', { name: 'Run study' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/^Running:/)).toBeTruthy();
  });

  it.each([
    ['feature', '/data-lab-docs'],
    ['signal', '/docs/signal-engine-methodology'],
    ['indicator-reliability', '/docs/indicator-reliability-methodology#1-context-and-scope'],
  ] as const)('opens the document that explains %s in a new tab, so the study keeps its inputs', async (study, address) => {
    await renderPage({ study });

    const link = screen.getByRole('link', { name: 'How this works' });
    expect(link.getAttribute('href')).toBe(address);
    expect(link.getAttribute('target')).toBe('_blank');
  });

  it.each(['cross-sectional', 'realized-vs-iv'] as const)('offers no help link for %s, which has no document', async (study) => {
    await renderPage({ study });

    expect(screen.queryByRole('link', { name: 'How this works' })).toBeNull();
  });

  it('shows the idle state until there is something to show', async () => {
    await renderPage({ study: 'feature' });

    expect(screen.getByText('No result yet.')).toBeTruthy();
    expect(screen.queryByTestId('study-notes')).toBeNull();
  });

  it('follows a result with its limitations and its next step', async () => {
    await renderPage({ study: 'feature', hasOutput: true, hasResult: true });

    expect(screen.queryByText('No result yet.')).toBeNull();
    expect(screen.getByText('the result')).toBeTruthy();
    const notes = within(screen.getByTestId('study-notes'));
    expect(notes.getByRole('heading', { name: 'Limitations' })).toBeTruthy();
    expect(notes.getByRole('heading', { name: 'Next step' })).toBeTruthy();
    expect(notes.getByRole('link', { name: 'Open the Signal study' }).getAttribute('href')).toBe('/research/signal');
  });

  it('marks a result as previous, with the inputs it answers, once the inputs change', async () => {
    await renderPage({ study: 'feature', hasOutput: true, hasResult: true, previous: true });

    expect(screen.getByText('Previous result')).toBeTruthy();
    expect(screen.getByText('SPY')).toBeTruthy();
  });

  it('says a descriptive study grades nothing', async () => {
    await renderPage({ study: 'realized-vs-iv', hasOutput: true, hasResult: true });
    expect(screen.getByTestId('study-descriptive-note')).toBeTruthy();
  });

  it('does not call an assessed study descriptive', async () => {
    await renderPage({ study: 'signal', hasOutput: true, hasResult: true });

    expect(screen.queryByTestId('study-descriptive-note')).toBeNull();
  });

  it.each(RESEARCH_STUDIES)('offers every study the list of its own saved runs: %s', async (study) => {
    await renderPage({ study: study.id });

    expect(screen.getByRole('link', { name: 'Saved runs' }).getAttribute('href')).toBe(
      `/research/history?type=${study.history}`,
    );
  });

  it('says when the inputs were filled in from a saved run, that nothing has run, and where that run is', async () => {
    await renderPage({
      study: 'feature',
      prefill: { state: 'filled', study: 'feature-study', id: 'a'.repeat(32), savedAtMs: 1_790_000_000_000 },
    });

    const notice = screen.getByTestId('study-prefill');
    expect(notice.textContent).toContain('Nothing has run');
    expect(within(notice).getByRole('link', { name: 'Open that run' }).getAttribute('href')).toBe(
      `/research/history/feature-study/${'a'.repeat(32)}`,
    );
    expect((screen.getByRole('button', { name: 'Run study' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('says why nothing was filled in when the saved run could not be used', async () => {
    await renderPage({ study: 'feature', prefill: { state: 'failed', message: 'The saved run to fill in from was not found.' } });

    expect(screen.getByRole('alert').textContent).toContain('was not found');
  });
});
