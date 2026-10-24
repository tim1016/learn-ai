import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { RouterOutlet } from '@angular/router';

import { DataLabWorkspaceStore, createDataLabWorkspaceStore, seedDefaultIndicators } from '../../components/data-lab/data-lab-workspace-store';
import { HowThisWorksLinkComponent } from '../../shared/help/how-this-works-link.component';
import { formatReceiptLabel } from '../../shared/pipes/receipt-label.pipe';
import { HealthNoticeComponent } from '../../shared/scoped-health/health-notice.component';
import { HEALTH_NEEDS } from '../../shared/scoped-health/scoped-health';
import { RunDockComponent } from '../../shared/run-dock/run-dock.component';
import { RUN_DOCK_SOURCE, RUN_DOCK_STORAGE_KEY } from '../../shared/run-dock/run-dock-source';
import { RunSessionService } from '../../services/run-session.service';
import type { WorkspaceAction } from '../../shared/workspace/workspace-action';
import { WorkspaceHeaderComponent, type WorkspaceBreadcrumbItem } from '../../shared/workspace/workspace-header.component';
import { WorkspaceStateBlockComponent } from '../../shared/workspace/workspace-state-block.component';
import { WorkspaceTabListComponent, type WorkspaceTabItem } from '../../shared/workspace/workspace-tab-list.component';
import type { WorkspaceStateContent } from '../../shared/workspace/workspace-state';
import { CurrentUrlService } from '../../shell/current-url.service';
import { menuLabelFor } from '../../shell/app-menu';
import { MARKETS_ROUTE, instrumentCommands, instrumentTabOf } from '../instrument-address';
import { InstrumentAdmissionService, type InstrumentAdmission } from '../instrument-admission.service';
import { InstrumentFactsHeaderComponent } from '../facts/instrument-facts-header.component';
import { InstrumentDataStore } from '../data/instrument-data.store';
import { ReturnsStudyHold } from '../returns/returns-study-hold';
import { unreadQueryKeys } from '../instrument-context';
import { InstrumentContextService } from '../instrument-context.service';
import { INSTRUMENT_TABS, UNAVAILABLE_INSTRUMENT_TABS } from '../instrument-tabs';
import { SavedSetupsComponent } from '../saved-setups/saved-setups.component';
import { SavedSetupsService } from '../saved-setups/saved-setups.service';
import { InstrumentContextBarComponent } from './instrument-context-bar.component';
import { MigrationNoticeComponent } from './migration-notice.component';

const BACK_TO_MARKETS = { id: 'markets', label: 'Back to Markets', route: [MARKETS_ROUTE] } as const;

/** The tabs whose addresses say which query keys they ignore; another tab (Options) reads keys of its own. */
const TABS_THAT_NAME_IGNORED_KEYS: ReadonlySet<string> = new Set(['chart', 'returns', 'data']);

/**
 * One instrument's place (ADR 0084): `/markets/:symbol/<tab>`.
 *
 * It owns what the tabs share and nothing they compute: the shared header, the
 * tab strip declared once, the pickers that commit the context, the workspace
 * store the tabs read and the one coverage gate that decides whether the
 * instrument opens. It is provided its own store, context and admission, and
 * the answers the Returns and Data tabs hold, so they live as long as the
 * place does: a tab change keeps them, and so does a move to another
 * instrument.
 *
 * It also hosts what belongs to the instrument and not to a tab: Build dataset
 * (an action in the header, a page under Data), the saved setups, and the run
 * dock, so a dataset job started on one tab stays in view on the others.
 *
 * An address that names no instrument the lake can hold fails in place, with
 * the way back to Markets. Nothing opens in its place.
 */
@Component({
  selector: 'app-instrument-workspace',
  imports: [
    RouterOutlet,
    InstrumentFactsHeaderComponent,
    HowThisWorksLinkComponent,
    HealthNoticeComponent,
    WorkspaceHeaderComponent,
    WorkspaceStateBlockComponent,
    WorkspaceTabListComponent,
    InstrumentContextBarComponent,
    MigrationNoticeComponent,
    SavedSetupsComponent,
    RunDockComponent,
  ],
  providers: [
    // Component-scoped by construction: the store dies with the place.
    { provide: DataLabWorkspaceStore, useFactory: createDataLabWorkspaceStore },
    InstrumentContextService,
    InstrumentAdmissionService,
    SavedSetupsService,
    // What the Returns and Data tabs hold, so a tab change asks for nothing it already has.
    ReturnsStudyHold,
    InstrumentDataStore,
    // The dock shows the dataset job Build dataset starts. Its expanded/collapsed choice keeps the key
    // the Data Lab used, so a person's saved choice is still read.
    { provide: RUN_DOCK_SOURCE, useExisting: RunSessionService },
    { provide: RUN_DOCK_STORAGE_KEY, useValue: 'run-dock-expanded:data-lab' },
  ],
  templateUrl: './instrument-workspace.component.html',
  styleUrl: './instrument-workspace.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class InstrumentWorkspaceComponent {
  private readonly context = inject(InstrumentContextService);
  private readonly admission = inject(InstrumentAdmissionService);
  private readonly url = inject(CurrentUrlService).url;
  private readonly store = inject(DataLabWorkspaceStore);
  protected readonly setups = inject(SavedSetupsService);

  protected readonly symbol = this.context.symbol;
  protected readonly admitted = this.admission.state;
  /** The instrument's catalog row once the place can open; its identity and held span come from it. */
  protected readonly admittedRow = computed(() => {
    const admitted = this.admitted();
    return admitted.kind === 'ready' ? admitted.row : null;
  });
  protected readonly contextProblems = computed(() => {
    const read = this.context.read();
    return read.kind === 'invalid' ? read.problems : [];
  });

  protected readonly breadcrumb = computed<readonly WorkspaceBreadcrumbItem[]>(() => [
    { label: menuLabelFor(MARKETS_ROUTE), route: [MARKETS_ROUTE] },
    { label: this.symbol() },
  ]);

  protected readonly activeTab = computed(() => instrumentTabOf(this.url()));

  protected readonly setupsOpen = signal(false);

  protected readonly healthNeeds = HEALTH_NEEDS.stockHistory;
  /**
   * The symbol the lake notice speaks for: the committed one, and only while the committed scope reads the
   * split-adjusted view, the one view the health projection covers. A raw view gets no notice rather than
   * another tree's finding.
   */
  protected readonly healthInstrument = computed(() => {
    const scope = this.store.committedScope();
    return scope?.adjusted ? scope.ticker : null;
  });

  /** Build dataset is an action on the instrument, so it is in the header on every tab; it opens only for an instrument that opens. */
  protected readonly headerActions = computed<readonly WorkspaceAction[]>(() =>
    this.admitted().kind === 'ready'
      ? [
          { id: 'saved-setups', label: 'Saved setups' },
          {
            id: 'build-dataset',
            label: 'Build dataset',
            route: instrumentCommands(this.symbol(), 'data', ['build']),
            queryParams: this.context.carriedQuery(),
          },
        ]
      : [],
  );

  /** What the Chart, Returns and Data tabs do not read in this address, as the notes the old Data Lab gave for a link it migrated. */
  private readonly dismissedKeys = signal('');
  protected readonly ignoredKeys = computed(() => {
    const tab = this.activeTab();
    return tab !== null && TABS_THAT_NAME_IGNORED_KEYS.has(tab) ? unreadQueryKeys(this.context.queryKeys()) : [];
  });
  protected readonly ignoredNotes = computed(() => {
    const keys = this.ignoredKeys();
    return keys.join('|') === this.dismissedKeys() ? [] : keys.map((key) => `Ignored unknown query key "${key}"`);
  });

  /** What stands in place of the tabs while a saved setup is being opened. */
  protected readonly openingState: WorkspaceStateContent = { kind: 'checking', happened: 'Opening the saved setup.' };

  /** Every declared tab; one this place does not route yet stays in the strip, greyed, with its reason. */
  protected readonly tabs = computed<readonly WorkspaceTabItem[]>(() => {
    const symbol = this.symbol();
    const carried = this.context.carriedQuery();
    return INSTRUMENT_TABS.tabs.map((tab) => {
      const reason = UNAVAILABLE_INSTRUMENT_TABS[tab.id];
      return reason === undefined
        ? { ...tab, route: instrumentCommands(symbol, tab.id), queryParams: carried }
        : { ...tab, route: null, unavailableReason: reason };
    });
  });

  /** What stands in place of the tabs while the instrument cannot open. */
  protected readonly stateContent = computed<WorkspaceStateContent | null>(() =>
    admissionState(this.symbol(), this.admitted()),
  );

  /** The window the address names but cannot use, said before any tab is asked for data. */
  protected readonly contextState = computed<WorkspaceStateContent | null>(() => {
    const problems = this.contextProblems();
    if (problems.length > 0) {
      return {
        kind: 'invalid',
        happened: 'The window in this address cannot be used, so nothing is loaded.',
        why: problems.join(' '),
        next: 'Pick a window above. Nothing is substituted for it.',
      };
    }
    const pending = this.context.defaultWindowPending();
    if (pending === null) return null;
    return pending.kind === 'resolving'
      ? { kind: 'checking', happened: 'Reading the default window for this instrument.' }
      : {
          kind: 'unavailable',
          happened: 'No default window could be read for this instrument.',
          why: pending.reason,
          next: 'Pick a window above to open the chart.',
        };
  });

  constructor() {
    // A fresh place starts with the product's default indicator recipe; seeding fetches nothing.
    seedDefaultIndicators(this.store);
  }

  protected onHeaderAction(id: string): void {
    if (id === 'saved-setups') this.setupsOpen.update((open) => !open);
  }

  protected dismissIgnored(): void {
    this.dismissedKeys.set(this.ignoredKeys().join('|'));
  }

  protected onAction(id: string): void {
    if (id === 'fill') this.admission.fill();
    else if (id === 'retry-fill') this.admission.retry();
    else if (id === 'dismiss-fill') this.admission.dismiss();
    else if (id === 'retry-catalog') this.admission.retryCatalog();
  }
}

/** What an instrument that cannot open says, in the words of the gate and the catalog. */
function admissionState(symbol: string, admission: InstrumentAdmission): WorkspaceStateContent | null {
  switch (admission.kind) {
    case 'ready':
      return null;
    case 'checking':
      return { kind: 'checking', happened: `Checking whether the lake holds ${symbol}.` };
    case 'not-a-symbol':
      return {
        kind: 'invalid',
        happened: `"${symbol}" is not a symbol this place can open.`,
        why: 'Markets holds US-equity symbols: one to five capital letters, with an optional class letter after a dot or a dash.',
        next: 'Pick an instrument from the Markets list, or search for it there.',
        actions: [BACK_TO_MARKETS],
      };
    case 'unlisted':
      return {
        kind: 'unavailable',
        happened: `${symbol} is not in the lake and is not a listed US-equity symbol.`,
        next: 'Search Markets by its name, or check the symbol.',
        actions: [BACK_TO_MARKETS],
      };
    case 'unavailable':
      return {
        kind: 'unavailable',
        happened: `Whether ${symbol} can be opened is not known: the symbol catalog could not be read.`,
        why: admission.reason,
        next: 'Read the catalog again, or go back to Markets.',
        actions: [{ id: 'retry-catalog', label: 'Read the catalog again' }, BACK_TO_MARKETS],
      };
    case 'unheld':
      return {
        kind: 'idle',
        happened: `The lake holds no bars for ${symbol}.`,
        next: 'Fill its history, and it opens here when the lake confirms the bars.',
        actions: [{ id: 'fill', label: 'Fill history, then open' }, BACK_TO_MARKETS],
      };
    case 'filling':
      return {
        kind: 'filling',
        happened: `${symbol} is not in the lake. Its trade bars are being filled before it opens.`,
        progress: { kind: 'phase', label: admission.gate.percent === null ? 'Backfill running' : `Backfill running, ${admission.gate.percent}% of its steps done` },
        next: 'It opens here when the lake confirms the bars. Cancelling leaves the lake as it is.',
        actions: [{ id: 'dismiss-fill', label: 'Cancel the fill' }],
      };
    case 'fill-failed':
      return {
        kind: 'failed',
        happened: `${symbol} could not be filled, so it does not open.`,
        why: `${formatReceiptLabel(admission.gate.reason)}: ${admission.gate.message ?? 'No detail was reported.'}`,
        next: 'Retry the fill, or go back to Markets.',
        actions: [
          ...(admission.gate.reason === 'view_not_backfillable' ? [] : [{ id: 'retry-fill', label: 'Retry the fill' }]),
          { id: 'dismiss-fill', label: 'Dismiss' },
          BACK_TO_MARKETS,
        ],
      };
  }
}
