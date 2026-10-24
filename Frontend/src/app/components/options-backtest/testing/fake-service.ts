import { PositionPreviewService } from '../position/position-preview.service';
import { quietPositionPreview } from '../position/testing';
import { render } from '@testing-library/angular';
import { screen } from '@testing-library/angular';
import type { Mock, vi as Vi } from 'vitest';

import { JobsService } from '../../../services/jobs.service';
import { FileDownloadService } from '../../../shared/file/file-download.service';
import { etIsoDate } from '../../../shared/date/et-midnight';
import { fakePickerWorld } from '../../../shared/symbol-picker/testing/fake-picker-world';
import { OptionsBacktestPageComponent } from '../options-backtest-page.component';
import { OptionsBacktestService, RunRefusedError, SetupInvalidError, type RunQuery } from '../options-backtest.service';
import { RUN_POLL_MS } from '../run-poll';
import { RunDetailService } from '../run-detail/run-detail.service';
import { inertRunDetail, type FakeRunDetail } from '../run-detail/testing/fake-run-detail';
import type { DataCheck, RunDetail, RunListItem, RunStartRequest, SavedSetup, SessionClock, SetupLimits, SetupRequest, SetupSummary } from '../options-backtest.types';
import { fakeJobs, type FakeJobs } from './fake-jobs';
import { dataCheck, readBackOf, SERVED_LIMITS, SESSION_CLOCK, servedExamples, sessionCheck } from './fixtures';

/** The `vi` of the spec that calls: a shared helper never imports vitest itself. */
type Spy = Pick<typeof Vi, 'fn'>;

export interface FakeService {
  sessionClock: Mock<() => Promise<SessionClock>>;
  limits: Mock<() => Promise<SetupLimits>>;
  checkData: Mock<(setup: SetupRequest, startMs: number, endMs: number) => Promise<DataCheck>>;
  startRun: Mock<(request: RunStartRequest) => Promise<string>>;
  runs: Mock<(query?: RunQuery) => Promise<RunListItem[]>>;
  run: Mock<(id: number) => Promise<RunDetail>>;
  deleteRun: Mock<(id: number) => Promise<void>>;
  describe: Mock<(body: unknown) => Promise<SetupSummary>>;
  list: Mock<() => Promise<SavedSetup[]>>;
  save: Mock<(name: string, setup: SetupRequest) => Promise<SavedSetup>>;
  remove: Mock<(name: string) => Promise<void>>;
}

/** The sentences a body gets in a spec, and the setup as read back; a stand-in for the server's own wording, which no spec pins. */
export function sentencesFor(body: SetupRequest): SetupSummary {
  return {
    window: [`Window for ${body.underlying}.`],
    legs: body.legs.map((leg) => ({ leg_id: leg.leg_id, sentences: [`Sentence for leg ${leg.leg_id}.`] })),
    square_off: [`Square off is ${body.square_off}.`],
    costs: [`Costs start from ${body.starting_cash_usd}.`],
    notices: [],
    setup: readBackOf(body),
  };
}

const MINUTE = 60_000;

/**
 * The rules the setup schema states about the setup as a whole (`setup.py`):
 * the route says each at `["body"]`, with no field, after the fields have all
 * passed. Null when the setup breaks none.
 */
function wholeSetupRefusal(setup: SetupRequest): string | null {
  const ids = setup.legs.map((leg) => leg.leg_id);
  const repeated = [...new Set(ids.filter((id, at) => ids.indexOf(id) !== at))].sort();
  if (repeated.length > 0) return `every leg needs its own id; repeated: ${repeated.join(', ')}`;
  const { entry, exit } = setup;
  if (entry.anchor === 'open' && entry.offset_ms < MINUTE) return 'the entry time must be at least one minute after the open';
  if (entry.anchor === 'close' && entry.offset_ms < 2 * MINUTE) {
    return 'the entry time must be at least two minutes before the close, to leave room for the exit';
  }
  if (exit.anchor === 'close' && exit.offset_ms < MINUTE) return 'the exit time must be at least one minute before the close';
  if (exit.anchor === 'open' && exit.offset_ms < 2 * MINUTE) {
    return 'the exit time must be at least two minutes after the open, to come after an entry';
  }
  if (entry.anchor === exit.anchor) {
    const exitIsLater = entry.anchor === 'open' ? exit.offset_ms > entry.offset_ms : exit.offset_ms < entry.offset_ms;
    if (!exitIsLater) return 'the exit time must be after the entry time';
  }
  return coverRefusal(setup);
}

/** A sold contract needs a bought one of its kind to cover it, and a setup that sells has one expiry rule on every leg. */
function coverRefusal(setup: SetupRequest): string | null {
  const contracts = (right: 'call' | 'put', side: 'buy' | 'sell') =>
    setup.legs.filter((leg) => leg.right === right && (leg.side ?? 'buy') === side).reduce((sum, leg) => sum + leg.contracts, 0);
  for (const right of ['call', 'put'] as const) {
    const sold = contracts(right, 'sell');
    const bought = contracts(right, 'buy');
    if (sold > bought) {
      return `the legs sell more ${right} contracts than they buy (${sold} sold, ${bought} bought): a sold ${right} needs a bought ${right} to cover it, so buy at least as many ${right} contracts as are sold`;
    }
  }
  const [first] = setup.legs;
  const differing = setup.legs.find((leg) => JSON.stringify(leg.expiry) !== JSON.stringify(first?.expiry));
  if (setup.legs.some((leg) => leg.side === 'sell') && differing !== undefined) {
    return `a setup with a sold leg needs every leg on one expiry rule, so that what is sold and what covers it expire together; leg '${differing.leg_id}' differs from leg '${first?.leg_id}'`;
  }
  return null;
}

/**
 * A service that answers as the real routes do where it matters: a setup with
 * no legs is a 422 at `legs`, and a setup that breaks a rule about the whole
 * setup is a 422 at `["body"]`, as the schema says, rather than a happy summary.
 */
export function fakeService(spy: Spy, changes: Partial<FakeService> = {}): FakeService {
  return {
    sessionClock: spy.fn(async () => SESSION_CLOCK),
    limits: spy.fn(async () => SERVED_LIMITS),
    // Unless a spec says otherwise, the first session of the range is ready.
    checkData: spy.fn(async (_setup: SetupRequest, startMs: number) => dataCheck([sessionCheck(etIsoDate(startMs))])),
    // Unless a spec says otherwise, a run starts as job-1, no run is going, and no run is found.
    startRun: spy.fn(async () => 'job-1'),
    runs: spy.fn(async () => []),
    run: spy.fn(async (id: number) => {
      throw new RunRefusedError('OPTIONS_BACKTEST_RUN_NOT_FOUND', `No run ${id}.`, 404);
    }),
    deleteRun: spy.fn(async () => undefined),
    describe: spy.fn(async (body: unknown) => {
      const setup = body as SetupRequest;
      if (setup.legs.length === 0) {
        throw new SetupInvalidError([{ loc: ['body', 'legs'], msg: 'List should have at least 1 item after validation, not 0' }]);
      }
      const refusal = wholeSetupRefusal(setup);
      if (refusal !== null) throw new SetupInvalidError([{ loc: ['body'], msg: `Value error, ${refusal}` }]);
      return sentencesFor(setup);
    }),
    list: spy.fn(async () => servedExamples()),
    save: spy.fn(async (name: string, setup: SetupRequest) => ({
      name,
      example: false,
      saved_at_ms: 1_760_000_000_000,
      setup: setup as SavedSetup['setup'],
      cannot_open_reason: null,
    })),
    remove: spy.fn(async () => undefined),
    ...changes,
  };
}

export interface FakeDownload {
  save: Mock<(blob: Blob, filename: string) => void>;
}

/**
 * `settled: false` returns as soon as the page is up, for a spec whose fake never answers (a read that is still going).
 * `jobs` is the jobs service a spec scripts; without one the page gets a jobs service that knows of no job.
 * The poll that reads a going run again is off unless `polling` is set, so a spec that is not about it never waits on a timer.
 */
export async function renderPage(service: FakeService, download?: FakeDownload, options: { setupName?: string; settled?: boolean; jobs?: FakeJobs; detail?: FakeRunDetail; polling?: boolean } = {}) {
  const view = await render(OptionsBacktestPageComponent, {
    inputs: { describeDebounceMs: 0, setup_name: options.setupName },
    providers: [
      { provide: PositionPreviewService, useValue: quietPositionPreview },
      ...fakePickerWorld().providers,
      ...(options.polling === true ? [] : [{ provide: RUN_POLL_MS, useValue: 0 }]),
      { provide: OptionsBacktestService, useValue: service },
      { provide: JobsService, useValue: (options.jobs ?? fakeJobs()).service },
      { provide: RunDetailService, useValue: options.detail ?? inertRunDetail() },
      ...(download === undefined ? [] : [{ provide: FileDownloadService, useValue: download }]),
    ],
  });
  await screen.findByRole('heading', { name: 'Entry settings' });
  // The limits and the saved setups are read by now, so a spec starts from a page that knows them.
  if (options.settled !== false) await view.fixture.whenStable();
  return view;
}
