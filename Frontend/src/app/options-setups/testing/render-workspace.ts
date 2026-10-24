import { ChangeDetectionStrategy, Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter, Router, RouterOutlet, withComponentInputBinding } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import type { Mock } from 'vitest';

import { OptionsBacktestService } from '../../components/options-backtest/options-backtest.service';
import { PositionPreviewService } from '../../components/options-backtest/position/position-preview.service';
import { quietPositionPreview } from '../../components/options-backtest/position/testing';
import type { FakeService } from '../../components/options-backtest/testing/fake-service';
import { FileDownloadService } from '../../shared/file/file-download.service';
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

/** A router with only the Options setups place in it, as the app mounts it. */
export function provideOptionsSetupsPlace() {
  return [provideRouter([{ path: 'options-setups', children: OPTIONS_SETUPS_ROUTES }], withComponentInputBinding())];
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
  /** What the fleet's bots read answers. */
  bots?: OptionsBotsRead | (() => Promise<OptionsBotsRead>);
}

/** A read a spec answers with a value, or with a function when it needs to answer late or to fail. */
function reading<T extends object>(answer: T | (() => Promise<T>)): () => Promise<T> {
  return answer instanceof Function ? answer : async () => answer;
}

async function renderPlace(service: FakeService, url: string, download: FakeDownload | undefined, options: RenderOptions) {
  const view = await render(RouterHost, {
    providers: [
      ...provideOptionsSetupsPlace(),
      { provide: SETUP_DESCRIBE_DEBOUNCE_MS, useValue: 0 },
      { provide: PositionPreviewService, useValue: quietPositionPreview },
      ...fakePickerWorld().providers,
      { provide: OptionsBacktestService, useValue: service },
      ...(options.index === undefined ? [] : [{ provide: OptionsSetupIndexService, useValue: { read: reading(options.index) } }]),
      ...(options.bots === undefined ? [] : [{ provide: OptionsSetupBotsService, useValue: { read: reading(options.bots) } }]),
      ...(download === undefined ? [] : [{ provide: FileDownloadService, useValue: download }]),
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

/** Opens one tab of one setup, whichever it is, and waits for what the place reads. */
export async function renderTab(service: FakeService, name: string | null, tab: OptionsSetupTab, options: RenderOptions = {}) {
  const view = await renderPlace(service, setupUrl(name, tab), undefined, options);
  if (options.settled !== false) await view.fixture.whenStable();
  return view;
}

/** Goes to another setup's Define tab by its address, as a link from the list does. */
export async function openSetup(_view: unknown, name: string): Promise<void> {
  await TestBed.inject(Router).navigateByUrl(setupUrl(name, 'define'));
}
