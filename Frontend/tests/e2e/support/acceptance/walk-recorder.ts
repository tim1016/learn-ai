import type { Locator, Page, TestInfo } from '@playwright/test';

/**
 * Counts one acceptance walk the way slice 1 counted today's task paths
 * (#3491), so the redesign can be compared with that baseline:
 *
 * - **Page visit**: a change of route (URL path) or an address-bar load.
 *   Switching a view on the same path is a click, not a visit.
 * - **Click**: every click and select-choice, including the click that opens a
 *   menu or a picker, and a browser Back.
 * - **Typed**: one per text entry.
 * - **Re-select**: choosing again something already chosen earlier in the walk
 *   (a symbol, a window, a strategy, a setup).
 * - **Apply/Refresh press**: Apply scope, Load, Refresh and the like.
 *
 * A dead end is a page with no way forward; the walk reaching its stop point
 * is the proof there was none, so the recorder keeps the stop and the
 * assertions in the spec carry the rest.
 */
export interface WalkCounts {
  readonly walk: string;
  readonly pageVisits: number;
  readonly clicks: number;
  readonly typed: number;
  readonly reselects: number;
  readonly applyRefreshPresses: number;
  readonly path: readonly string[];
  readonly stop: string;
}

export class WalkRecorder {
  private visits = 0;
  private clicks = 0;
  private typed = 0;
  private reselects = 0;
  private presses = 0;
  private stopAt = '';
  private readonly path: string[] = [];
  private readonly chosen = new Map<string, string>();
  private lastPath = '';
  /** After an address load the app may redirect once, on its own; that is the same visit, so path changes wait for the next action. */
  private afterLoad = false;

  constructor(
    private readonly page: Page,
    readonly name: string,
  ) {
    page.on('framenavigated', (frame) => {
      if (frame !== page.mainFrame() || this.afterLoad) return;
      const url = new URL(frame.url());
      if (url.protocol === 'about:') return;
      if (url.pathname === this.lastPath) return;
      this.lastPath = url.pathname;
      this.visits += 1;
      this.path.push(url.pathname);
    });
  }

  /** An address-bar load: a visit even when the path is the one already open. */
  async open(url: string): Promise<void> {
    this.settle();
    this.visits += 1;
    this.afterLoad = true;
    await this.page.goto(url);
  }

  /** A browser Back: a click, and a visit when it changes the path. */
  async back(): Promise<void> {
    this.settle();
    this.clicks += 1;
    await this.page.goBack();
  }

  /** A browser reload of the open address: a visit of the same path. */
  async reload(): Promise<void> {
    this.settle();
    this.visits += 1;
    this.afterLoad = true;
    await this.page.reload();
  }

  async click(target: Locator): Promise<void> {
    this.settle();
    this.clicks += 1;
    await target.click();
  }

  /** A press of Apply, Load, Refresh or the like: a click that the redesign promises not to need. */
  async press(target: Locator): Promise<void> {
    this.presses += 1;
    await this.click(target);
  }

  async type(target: Locator, text: string): Promise<void> {
    this.settle();
    this.typed += 1;
    await target.fill(text);
  }

  /** Records choosing `value` for `what`; choosing again what the walk already chose counts as a re-select. */
  choose(what: string, value: string): void {
    if (this.chosen.get(what) === value) this.reselects += 1;
    this.chosen.set(what, value);
  }

  /** Marks where the walk stops, which is before any consequential command. */
  stopsAt(stop: string): void {
    this.stopAt = stop;
  }

  /** Takes the address an address load ended on, redirects included, as that load's one visit. */
  private settle(): void {
    if (!this.afterLoad) return;
    this.afterLoad = false;
    this.lastPath = new URL(this.page.url()).pathname;
    this.path.push(this.lastPath);
  }

  counts(): WalkCounts {
    this.settle();
    return {
      walk: this.name,
      pageVisits: this.visits,
      clicks: this.clicks,
      typed: this.typed,
      reselects: this.reselects,
      applyRefreshPresses: this.presses,
      path: [...this.path],
      stop: this.stopAt,
    };
  }

  /** Attaches the counts to the test report, where a run's JSON reporter carries them out. */
  async report(info: TestInfo): Promise<WalkCounts> {
    const counts = this.counts();
    await info.attach(`${this.name}-counts.json`, { body: JSON.stringify(counts, null, 2), contentType: 'application/json' });
    return counts;
  }
}
