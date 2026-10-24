import { Injectable, computed, effect, inject, signal, untracked } from '@angular/core';
import { Router, type Params } from '@angular/router';

import {
  DATA_LAB_WORKSPACE_SCHEMA_VERSION,
  DataLabWorkspaceStore,
} from '../../components/data-lab/data-lab-workspace-store';
import { utcMsToIsoDate } from '../../components/data-lab/data-lab-request-mapper';
import {
  DataLabSessionService,
  type DataLabSessionConfig,
  type DataLabSessionSummary,
} from '../../services/data-lab-session.service';
import { chartRequestIdentity } from '../chart/chart-request';
import { MARKETS_ROUTE, instrumentPageOf } from '../instrument-address';
import { ADJUSTMENT_QUERY_KEY, DEFAULT_ADJUSTMENT, DEFAULT_FORWARD_FILL, FORWARD_FILL_QUERY_KEY, SETUP_QUERY_KEY } from '../instrument-context';
import { InstrumentContextService } from '../instrument-context.service';
import { isShowableChart, planSetupOpening, type SetupOpening } from './saved-setup';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * An instrument place's saved setups (PRD #3490 R10, R33): the records Python
 * holds at `/api/data-lab/sessions`, listed, saved, renamed, deleted and
 * opened under the instrument they are for.
 *
 * Opening one is the only thing that reads a whole record, and it reads one
 * record, once. It gives the workspace the setup's recipe, scope and saved
 * chart through the store's own validator, then moves the address to the
 * setup's instrument and window; the tab that shows it then holds the answer
 * to its request already, and asks for nothing. The address can ask for the
 * same thing (`?sessionId=<id>`, which a link from the old Data Lab carries):
 * the place opens the setup, and the address that results has no `sessionId`,
 * so Back, Forward and a reload never open it a second time.
 *
 * The records are never rewritten by opening them. Whatever this page cannot
 * take from one is named in `notes` and stays in the record.
 */
@Injectable()
export class SavedSetupsService {
  private readonly store = inject(DataLabWorkspaceStore);
  private readonly sessions = inject(DataLabSessionService);
  private readonly context = inject(InstrumentContextService);
  private readonly router = inject(Router);

  readonly list = signal<readonly DataLabSessionSummary[]>([]);
  /** The name the Save setup field holds. */
  readonly name = signal('');
  /** Why the last call failed; cleared when the next one starts. */
  readonly error = signal<string | null>(null);
  /** What opening the last setup could not take from its record. */
  readonly notes = signal<readonly string[]>([]);
  readonly saving = signal(false);
  private readonly opening = signal(false);
  /** The address being opened, so the same one is not opened twice at once. */
  private opened: string | null = null;
  /** The latest opening: an answer to an earlier one is not this page's any more. */
  private turn = 0;

  /** Saving needs the committed scope: a setup is never built from a draft the chart did not follow. */
  readonly canSave = computed(() => this.store.committedScope() !== null);

  /**
   * The setup that "Update setup" would replace: the one this workspace was
   * opened from or saved as, while the place is still on that setup's
   * instrument. Another instrument's setup is never overwritten by saving here.
   */
  readonly activeId = computed(() => {
    const ref = this.store.savedSession();
    if (ref === null) return null;
    const row = this.list().find((candidate) => candidate.id === ref.id);
    return row !== undefined && row.ticker.toUpperCase() === this.store.committedTicker() ? ref.id : null;
  });

  /** A setup is being opened, so what the tabs would show is not the setup's yet. */
  readonly pending = computed(() => this.opening() || this.addressSetup() !== null);

  /** The setup the address names, when what it names is a setup id. */
  private readonly addressSetup = computed(() => {
    const named = this.context.setupParam();
    return named !== null && UUID.test(named) ? named : null;
  });

  constructor() {
    effect(() => {
      const named = this.context.setupParam();
      if (named !== null) untracked(() => void this.openFromAddress(named));
    });
  }

  // ── Reading and changing the records ────────────────────────

  async refresh(): Promise<void> {
    const listed = await this.attempt(() => this.sessions.listSessions());
    if (listed.ok) this.list.set(listed.value);
  }

  async save(): Promise<void> {
    const config = this.configOfWorkspace();
    if (config === null) return;
    this.saving.set(true);
    this.error.set(null);
    try {
      // The chart goes in only while it still answers the committed scope and recipe: a stale
      // one depicts a setup this page no longer has, so the record is saved without it.
      const latest = this.store.latestChartSnapshot();
      const chart = !this.store.chartStale() && isShowableChart(latest) ? latest : null;
      const activeId = this.activeId();
      await this.attempt(async () => {
        if (activeId !== null) {
          await this.sessions.updateSession(activeId, config, chart, this.name() || config.ticker);
        } else {
          const id = await this.sessions.saveSession(config, chart, this.name() || undefined);
          this.store.setSavedSession({ id, schemaVersion: DATA_LAB_WORKSPACE_SCHEMA_VERSION });
        }
      });
      await this.refresh();
    } finally {
      this.saving.set(false);
    }
  }

  async saveAsNew(): Promise<void> {
    this.store.setSavedSession(null);
    await this.save();
  }

  detach(): void {
    this.store.setSavedSession(null);
    this.name.set('');
  }

  async rename(id: string, name: string): Promise<void> {
    this.error.set(null);
    await this.attempt(() => this.sessions.renameSession(id, name));
    await this.refresh();
  }

  async remove(id: string): Promise<void> {
    this.error.set(null);
    const deleted = await this.attempt(() => this.sessions.deleteSession(id));
    if (deleted.ok && this.store.savedSession()?.id === id) this.detach();
    await this.refresh();
  }

  // ── Opening one ─────────────────────────────────────────────

  /**
   * Opens the setup `id` under its instrument. From the drawer the address
   * moves forward; from an address that named the setup it is replaced, so the
   * link that asked is not left in the history.
   */
  async open(id: string, fromAddress = false): Promise<void> {
    const turn = ++this.turn;
    this.opening.set(true);
    this.error.set(null);
    try {
      const loaded = await this.attempt(() => this.sessions.getSession(id));
      // A later opening replaced this one while its record was being read.
      if (!loaded.ok || turn !== this.turn) return;
      const session = loaded.value;
      if (session === undefined) {
        this.error.set('Session not found');
        await this.refresh();
        return;
      }
      const plan = planSetupOpening(session, this.context.shown().timeframe, this.store.companions());
      const colors = this.store.colorTokenOverrides();
      const restored = this.store.restore(plan.envelope);
      // A colour chosen for an indicator the setup also holds stays chosen.
      for (const [instance, token] of Object.entries(colors)) this.store.setColorToken(instance, token);
      this.holdChart(plan, restored.warnings.length === 0 && plan.address.window !== null);
      this.notes.set([...plan.notes, ...restored.warnings]);
      this.name.set(session.name);
      await this.refresh();
      if (plan.ticker !== '' && turn === this.turn) await this.moveTo(plan, fromAddress);
    } finally {
      if (turn === this.turn) {
        this.opening.set(false);
        if (this.context.setupParam() !== null) await this.dropSetupParam();
      }
    }
  }

  clearNotes(): void {
    this.notes.set([]);
  }

  /**
   * Makes the saved chart the held answer to the request the opened setup
   * makes, so the Chart tab shows it without asking. That holds only when the
   * setup was taken whole: a recipe or scope that lost a part makes another
   * request than the one the chart answered. Without a chart, nothing the
   * workspace held before answers it: the chart is marked stale and the tab
   * asks, and a later save cannot carry another context's chart.
   */
  private holdChart(plan: SetupOpening, whole: boolean): void {
    const scope = this.store.committedScope();
    if (plan.chart !== null && whole && scope !== null) {
      this.store.setLatestChartSnapshot(plan.chart);
      this.store.recordChartRequest(chartRequestIdentity(scope, this.store.indicators()));
      return;
    }
    this.store.setLatestChartSnapshot(null);
    this.store.markChartStale();
  }

  /**
   * Moves the address to the setup's instrument and context (window, bar timeframe, session, price adjustment and
   * forward fill: the address is where the tabs read them, so a reload or Back keeps the setup's). A setup opened from
   * the drawer is a chart, so it opens on the Chart tab; one an address asked
   * for stays on the page that address named (Build dataset, Returns, Data).
   */
  private async moveTo(plan: SetupOpening, fromAddress: boolean): Promise<void> {
    const { window, timeframe, session, adjustment, forwardFill } = plan.address;
    const queryParams: Params = {
      timeframe,
      session,
      // The defaults are an absent key, as the place writes them.
      ...(adjustment === DEFAULT_ADJUSTMENT ? {} : { [ADJUSTMENT_QUERY_KEY]: adjustment }),
      ...(forwardFill === DEFAULT_FORWARD_FILL ? {} : { [FORWARD_FILL_QUERY_KEY]: forwardFill ? 'on' : 'off' }),
      ...(window === null ? {} : { from: window.from, to: window.to }),
    };
    const page = (fromAddress ? instrumentPageOf(this.router.url) : null) ?? ['chart'];
    await this.router.navigate([MARKETS_ROUTE, plan.ticker, ...page], { queryParams, replaceUrl: fromAddress });
  }

  private async openFromAddress(named: string): Promise<void> {
    if (!UUID.test(named)) {
      this.notes.set([`Ignored sessionId "${named}": it is not a saved setup's id (not a UUID).`]);
      await this.dropSetupParam();
      return;
    }
    if (this.opened === named) return;
    this.opened = named;
    try {
      await this.open(named, true);
    } finally {
      this.opened = null;
    }
  }

  /** Takes the setup out of the address, in place, so a setup that cannot be opened does not hold the page. */
  private async dropSetupParam(): Promise<void> {
    const tree = this.router.parseUrl(this.router.url);
    const { [SETUP_QUERY_KEY]: _spent, ...rest } = tree.queryParams;
    tree.queryParams = rest;
    await this.router.navigateByUrl(tree, { replaceUrl: true });
  }

  // ── The record a save writes ────────────────────────────────

  private configOfWorkspace(): DataLabSessionConfig | null {
    const scope = this.store.committedScope();
    if (scope === null) return null;
    return {
      ticker: scope.ticker,
      fromDate: utcMsToIsoDate(scope.window.startMsUtc),
      toDate: utcMsToIsoDate(scope.window.endMsUtc),
      session: scope.session === 'extended' ? 'extended' : 'rth',
      forwardFill: scope.forwardFill,
      adjusted: scope.adjusted,
      entries: this.store.indicators().map((indicator) => ({ name: indicator.canonicalKey, params: { ...indicator.params } })),
    };
  }

  /** One call to the records; a failure lands in `error` instead of leaving as an unhandled rejection from a click. */
  private async attempt<T>(call: () => Promise<T>): Promise<{ readonly ok: true; readonly value: T } | { readonly ok: false }> {
    try {
      return { ok: true, value: await call() };
    } catch (failure: unknown) {
      this.error.set(failure instanceof Error ? failure.message : 'The saved setups request failed.');
      return { ok: false };
    }
  }
}
