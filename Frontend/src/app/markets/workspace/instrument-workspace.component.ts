import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterOutlet } from '@angular/router';

import { DataLabWorkspaceStore, createDataLabWorkspaceStore, seedDefaultIndicators } from '../../components/data-lab/data-lab-workspace-store';
import { HowThisWorksLinkComponent } from '../../shared/help/how-this-works-link.component';
import { formatReceiptLabel } from '../../shared/pipes/receipt-label.pipe';
import { WorkspaceHeaderComponent, type WorkspaceBreadcrumbItem } from '../../shared/workspace/workspace-header.component';
import { WorkspaceStateBlockComponent } from '../../shared/workspace/workspace-state-block.component';
import { WorkspaceTabListComponent, type WorkspaceTabItem } from '../../shared/workspace/workspace-tab-list.component';
import type { WorkspaceStateContent } from '../../shared/workspace/workspace-state';
import { CurrentUrlService } from '../../shell/current-url.service';
import { menuLabelFor } from '../../shell/app-menu';
import { MARKETS_ROUTE, instrumentCommands, instrumentTabOf } from '../instrument-address';
import { InstrumentAdmissionService, type InstrumentAdmission } from '../instrument-admission.service';
import { InstrumentFactsHeaderComponent } from '../facts/instrument-facts-header.component';
import { InstrumentContextService } from '../instrument-context.service';
import { INSTRUMENT_TABS, UNAVAILABLE_INSTRUMENT_TABS } from '../instrument-tabs';
import { InstrumentContextBarComponent } from './instrument-context-bar.component';

const BACK_TO_MARKETS = { id: 'markets', label: 'Back to Markets', route: [MARKETS_ROUTE] } as const;

/**
 * One instrument's place (ADR 0084): `/markets/:symbol/<tab>`.
 *
 * It owns what the tabs share and nothing they compute: the shared header, the
 * tab strip declared once, the pickers that commit the context, the workspace
 * store the tabs read and the one coverage gate that decides whether the
 * instrument opens. It is provided its own store, context and admission, so
 * they live as long as the place does: a tab change keeps them, and so does a
 * move to another instrument.
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
    WorkspaceHeaderComponent,
    WorkspaceStateBlockComponent,
    WorkspaceTabListComponent,
    InstrumentContextBarComponent,
  ],
  providers: [
    // Component-scoped by construction: the store dies with the place.
    { provide: DataLabWorkspaceStore, useFactory: createDataLabWorkspaceStore },
    InstrumentContextService,
    InstrumentAdmissionService,
  ],
  templateUrl: './instrument-workspace.component.html',
  styleUrl: './instrument-workspace.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class InstrumentWorkspaceComponent {
  private readonly context = inject(InstrumentContextService);
  private readonly admission = inject(InstrumentAdmissionService);
  private readonly url = inject(CurrentUrlService).url;

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
    seedDefaultIndicators(inject(DataLabWorkspaceStore));
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
