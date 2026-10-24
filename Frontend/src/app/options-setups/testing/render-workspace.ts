import { ChangeDetectionStrategy, Component, type EnvironmentProviders, type Provider } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter, Router, RouterOutlet, withComponentInputBinding, type Routes } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import type { Mock } from 'vitest';

import { GoldenOptionSetupsService } from '../../components/options-backtest/golden-option-setups.service';
import { OptionsBacktestService } from '../../components/options-backtest/options-backtest.service';
import type { SavedSetup } from '../../components/options-backtest/options-backtest.types';
import { PastChainService } from '../../components/options-backtest/past-chain/past-chain.service';
import { OptionsEvidenceService } from '../../components/options-evidence/options-evidence.service';
import { OptionsGoldenSearchService } from '../../components/options-golden-search/options-golden-search.service';
import { PositionPreviewService } from '../../components/options-backtest/position/position-preview.service';
import { quietPositionPreview } from '../../components/options-backtest/position/testing';
import { RunDetailService } from '../../components/options-backtest/run-detail/run-detail.service';
import { inertRunDetail, type FakeRunDetail } from '../../components/options-backtest/run-detail/testing/fake-run-detail';
import { RUN_POLL_MS } from '../../components/options-backtest/run-poll';
import { fakeJobs, type FakeJobs } from '../../components/options-backtest/testing/fake-jobs';
import type { FakeService } from '../../components/options-backtest/testing/fake-service';
import { JobsService } from '../../services/jobs.service';
import { FileDownloadService } from '../../shared/file/file-download.service';
import { fakeScopedHealth, projection } from '../../shared/scoped-health/testing/scoped-health-fixtures';
import { fakePickerWorld } from '../../shared/symbol-picker/testing/fake-picker-world';
import { OptionsSetupIndexService, type OptionsSetupIndex } from '../index/options-setup-index.service';
import { OptionsSetupBotsService, type OptionsBotsRead } from '../workspace/overview/options-setup-bots.service';
import { OPTIONS_SETUPS_ROUTES } from '../options-setups.routes';
import { SETUP_DESCRIBE_DEBOUNCE_MS } from '../workspace/options-setup-store';
import { optionsSetupRoute, type OptionsSetupTab } from '../options-setups.address';

export interface FakeDownload {
  save: Mock<(blob: Blob, filename: string) => void>;
}

/** The address of one tab, as the router takes it: a saved name, or the unsaved draft. */
export function setupUrl(name: string | null, tab: OptionsSetupTab = 'define'): string {
  return optionsSetupRoute(name, tab)
    .map((segment, at) => (at === 0 ? segment : encodeURIComponent(segment)))
    .join('/');
}

/** A router with only the Options setups place in it, as the app mounts it, and the other places a spec walks to. */
export function provideOptionsSetupsPlace(otherPlaces: Routes = []) {
  return [provideRouter([{ path: 'options-setups', children: OPTIONS_SETUPS_ROUTES }, ...otherPlaces], withComponentInputBinding())];
}

/** The app's one outlet, so the place is drawn by the real router, in a fixture that testing-library's events refresh. */
@Component({ selector: 'app-options-setups-test-host', imports: [RouterOutlet], changeDetection: ChangeDetectionStrategy.OnPush, template: '<router-outlet />' })
class RouterHost {}

interface RenderOptions {
  /** The saved name in the address; none is the unsaved draft. */
  setupName?: string;
  /** `false` returns as soon as the page is up, for a spec whose fake never answers (a read that is still going). */
  settled?: boolean;
  /** `false` does not wait for the setup's cards, for a spec whose session clock never answers. */
  form?: boolean;
  /** What the evidence read answers, for the tabs that list evidence. */
  index?: OptionsSetupIndex | (() => Promise<OptionsSetupIndex>);
  /**
   * Saved names the library lists besides what the service lists, each with the hash of the content it holds, for a spec
   * about the evidence, which needs a name to hold a content and little else. A name the service lists too keeps its
   * setup and takes the hash given here. A function is asked each time the library is read, so a spec can move a hash
   * as a save does.
   */
  saved?: readonly SavedSetup[] | (() => readonly SavedSetup[]);
  /** What the fleet's bots read answers. */
  bots?: OptionsBotsRead | (() => Promise<OptionsBotsRead>);
  /** The jobs service a spec scripts; without one the place gets a jobs service that knows of no job. */
  jobs?: FakeJobs;
  /** What the run's detail reads answer, for the views that show a run's days, trades and comparison. */
  detail?: FakeRunDetail;
  /** The Past chain's own service, replaced the way the setup's service is. */
  chain?: object;
  /** What the retained-evidence service answers, for the views that read one run's evidence. */
  evidence?: object;
  /** What the approved versions' own service answers, for the Golden tab. */
  golden?: object;
  /** What the golden search's service answers, for the plan on the Golden tab. */
  search?: object;
  /** The poll that reads a going run again is off unless this is set, so a spec that is not about it never waits on a timer. */
  polling?: boolean;
  /** What the address says after the tab (`?run=7`). */
  query?: string;
  /** Other places in the app a spec walks to from here (the account list, an account's Deploy), declared as the app does. */
  places?: Routes;
  /** What those places read, replaced the way the setup's own services are. */
  providers?: (Provider | EnvironmentProviders)[];
}

/** A read a spec answers with a value, or with a function when it needs to answer late or to fail. */
function reading<T extends object>(answer: T | (() => Promise<T>)): () => Promise<T> {
  return answer instanceof Function ? answer : async () => answer;
}

/** The service as the place is given it: its library also lists the names a spec adds (`RenderOptions.saved`). */
function withSaved(service: FakeService, added: RenderOptions['saved']): object {
  if (added === undefined) return service;
  return {
    ...service,
    list: async (): Promise<SavedSetup[]> => {
      const saved = added instanceof Function ? added() : added;
      const listed = await service.list();
      const held = listed.map((one) => {
        const named = saved.find((each) => each.name === one.name);
        return named === undefined ? one : { ...one, setup_sha256: named.setup_sha256 };
      });
      return [...held, ...saved.filter((each) => !listed.some((one) => one.name === each.name))];
    },
  };
}

async function renderPlace(placeService: FakeService, url: string, download: FakeDownload | undefined, options: RenderOptions) {
  const service = withSaved(placeService, options.saved);
  const view = await render(RouterHost, {
    providers: [
      ...provideOptionsSetupsPlace(options.places),
      { provide: SETUP_DESCRIBE_DEBOUNCE_MS, useValue: 0 },
      { provide: PositionPreviewService, useValue: quietPositionPreview },
      ...fakePickerWorld().providers,
      // The data plane has nothing to say unless a spec scripts it, so its notice never stands in for what a spec is about.
      fakeScopedHealth(projection()).provider,
      ...(options.polling === true ? [] : [{ provide: RUN_POLL_MS, useValue: 0 }]),
      { provide: OptionsBacktestService, useValue: service },
      { provide: JobsService, useValue: (options.jobs ?? fakeJobs()).service },
      { provide: RunDetailService, useValue: options.detail ?? inertRunDetail() },
      ...(options.chain === undefined ? [] : [{ provide: PastChainService, useValue: options.chain }]),
      ...(options.evidence === undefined ? [] : [{ provide: OptionsEvidenceService, useValue: options.evidence }]),
      ...(options.golden === undefined ? [] : [{ provide: GoldenOptionSetupsService, useValue: options.golden }]),
      ...(options.search === undefined ? [] : [{ provide: OptionsGoldenSearchService, useValue: options.search }]),
      ...(options.index === undefined ? [] : [{ provide: OptionsSetupIndexService, useValue: { read: reading(options.index) } }]),
      ...(options.bots === undefined ? [] : [{ provide: OptionsSetupBotsService, useValue: { read: reading(options.bots) } }]),
      ...(download === undefined ? [] : [{ provide: FileDownloadService, useValue: download }]),
      ...(options.providers ?? []),
    ],
  });
  await TestBed.inject(Router).navigateByUrl(url);
  return view;
}

/**
 * Opens the Define tab of one setup, by its address, as the owner does: the
 * saved name in the URL, or the unsaved draft when there is none.
 */
export async function renderDefine(service: FakeService, download?: FakeDownload, options: RenderOptions = {}) {
  const view = await renderPlace(service, setupUrl(options.setupName ?? null, 'define'), download, options);
  if (options.form !== false) await screen.findByRole('heading', { name: 'Entry settings' });
  // The limits and the saved setups are read by now, so a spec starts from a page that knows them.
  if (options.settled !== false) await view.fixture.whenStable();
  return view;
}

/**
 * Opens one setup's Explore by its address, as the owner does: the saved name and, after it, what the address says besides
 * (`?view=history&run=30`). It waits for the saved setup to be read back, which is what the page's buttons wait for.
 */
export async function renderExplore(service: FakeService, name: string | null, options: RenderOptions & { query?: string } = {}) {
  const view = await renderPlace(service, `${setupUrl(name, 'explore')}${options.query ?? ''}`, undefined, options);
  if (options.settled !== false) await view.fixture.whenStable();
  return view;
}

/** Opens one tab of one setup, whichever it is, and waits for what the place reads. */
export async function renderTab(service: FakeService, name: string | null, tab: OptionsSetupTab, options: RenderOptions = {}) {
  const view = await renderPlace(service, `${setupUrl(name, tab)}${options.query ?? ''}`, undefined, options);
  if (options.settled !== false) await view.fixture.whenStable();
  return view;
}

/** Goes to another setup's Define tab by its address, as a link from the list does. */
export async function openSetup(_view: unknown, name: string): Promise<void> {
  await TestBed.inject(Router).navigateByUrl(setupUrl(name, 'define'));
}
