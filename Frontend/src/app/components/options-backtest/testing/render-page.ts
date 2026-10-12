import { render } from '@testing-library/angular';
import { screen } from '@testing-library/angular';
import type { Mock, vi as Vi } from 'vitest';

import { FileDownloadService } from '../../../shared/file/file-download.service';
import { fakePickerWorld } from '../../../shared/symbol-picker/testing/fake-picker-world';
import { OptionsBacktestPageComponent } from '../options-backtest-page.component';
import { OptionsBacktestService, SetupInvalidError } from '../options-backtest.service';
import type { SavedSetup, SessionClock, SetupRequest, SetupSummary } from '../options-backtest.types';
import { SESSION_CLOCK, servedExamples } from './fixtures';

/** The `vi` of the spec that calls: a shared helper never imports vitest itself. */
type Spy = Pick<typeof Vi, 'fn'>;

export interface FakeService {
  sessionClock: Mock<() => Promise<SessionClock>>;
  describe: Mock<(body: unknown) => Promise<SetupSummary>>;
  list: Mock<() => Promise<SavedSetup[]>>;
  save: Mock<(name: string, setup: SetupRequest) => Promise<SavedSetup>>;
  remove: Mock<(name: string) => Promise<void>>;
}

/** The sentences a body gets in a spec; a stand-in for the server's own wording, which no spec pins. */
export function sentencesFor(body: SetupRequest): SetupSummary {
  return {
    window: `Window for ${body.underlying}.`,
    legs: body.legs.map((leg) => ({ leg_id: leg.leg_id, text: `Sentence for leg ${leg.leg_id}.` })),
    square_off: `Square off is ${body.square_off}.`,
    costs: `Costs start from ${body.starting_cash_usd}.`,
    notices: [],
  };
}

/**
 * A service that answers as the real routes do where it matters: a setup with
 * no legs is a 422 at `legs`, as the schema says, rather than a happy summary.
 */
export function fakeService(spy: Spy, changes: Partial<FakeService> = {}): FakeService {
  return {
    sessionClock: spy.fn(async () => SESSION_CLOCK),
    describe: spy.fn(async (body: unknown) => {
      const setup = body as SetupRequest;
      if (setup.legs.length === 0) {
        throw new SetupInvalidError([{ loc: ['body', 'legs'], msg: 'List should have at least 1 item after validation, not 0' }]);
      }
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

export async function renderPage(service: FakeService, download?: FakeDownload) {
  const view = await render(OptionsBacktestPageComponent, {
    inputs: { describeDebounceMs: 0 },
    providers: [
      ...fakePickerWorld().providers,
      { provide: OptionsBacktestService, useValue: service },
      ...(download === undefined ? [] : [{ provide: FileDownloadService, useValue: download }]),
    ],
  });
  await screen.findByRole('heading', { name: 'Entry settings' });
  return view;
}
