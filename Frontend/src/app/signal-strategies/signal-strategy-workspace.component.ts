import { LowerCasePipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterOutlet } from '@angular/router';

import { CurrentUrlService } from '../shell/current-url.service';
import { HowThisWorksLinkComponent } from '../shared/help/how-this-works-link.component';
import { ExperimentalNoticeComponent } from '../shared/experimental-notice/experimental-notice.component';
import { ReceiptLabelPipe } from '../shared/pipes/receipt-label.pipe';
import type { WorkspaceAction } from '../shared/workspace/workspace-action';
import { WorkspaceHeaderComponent, type WorkspaceBreadcrumbItem } from '../shared/workspace/workspace-header.component';
import type { WorkspaceStateContent } from '../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../shared/workspace/workspace-state-block.component';
import { WorkspaceTabListComponent, type WorkspaceTabItem } from '../shared/workspace/workspace-tab-list.component';
import { PROOF_TONES, proofSummaryOf } from './signal-strategy-evidence';
import { SignalStrategyChipComponent } from './signal-strategy-chip.component';
import {
  SIGNAL_STRATEGIES_RECENCY_ROUTE,
  SIGNAL_STRATEGIES_ROUTE,
  SIGNAL_STRATEGY_TABS,
  UNAVAILABLE_TAB_REASONS,
  signalStrategyLocation,
  signalStrategyTabRoute,
} from './signal-strategy-tabs';
import { SignalStrategyWorkspaceStore } from './signal-strategy-workspace.store';

/**
 * One registered signal strategy's place (ADR 0084): its header and tabs, and
 * the page of the open tab beneath. The address names the registry key; a key
 * the registry does not list fails here, in place, with its way back, and no
 * tab page is opened for it. The store lives with this shell, so moving
 * between tabs reads nothing again.
 */
@Component({
  selector: 'app-signal-strategy-workspace',
  changeDetection: ChangeDetectionStrategy.OnPush,
  providers: [SignalStrategyWorkspaceStore],
  imports: [
    ExperimentalNoticeComponent,
    HowThisWorksLinkComponent,
    LowerCasePipe,
    ReceiptLabelPipe,
    RouterOutlet,
    SignalStrategyChipComponent,
    WorkspaceHeaderComponent,
    WorkspaceStateBlockComponent,
    WorkspaceTabListComponent,
  ],
  templateUrl: './signal-strategy-workspace.component.html',
  styleUrl: './signal-strategy-workspace.component.scss',
})
export class SignalStrategyWorkspaceComponent {
  protected readonly store = inject(SignalStrategyWorkspaceStore);
  private readonly currentUrl = inject(CurrentUrlService).url;

  protected readonly proofTones = PROOF_TONES;
  protected readonly summaryOf = proofSummaryOf;
  protected readonly activeTab = computed(() => signalStrategyLocation(this.currentUrl())?.tab ?? null);

  protected readonly title = computed(() => {
    const state = this.store.entry();
    if (state.kind === 'found') return state.entry.display_name;
    return state.kind === 'unknown' ? 'Unknown strategy' : this.store.key();
  });

  protected readonly breadcrumb = computed<readonly WorkspaceBreadcrumbItem[]>(() => [
    { label: 'Signal strategies', route: [SIGNAL_STRATEGIES_ROUTE] },
    { label: this.title() },
  ]);

  /** A Recency Chart filtered to this strategy, offered where the registry lets it be swept. */
  protected readonly actions = computed<readonly WorkspaceAction[]>(() => {
    const state = this.store.entry();
    if (state.kind !== 'found' || !state.entry.capabilities.sweep.supported) return [];
    return [
      {
        id: 'recency',
        label: 'Recency chart',
        route: [SIGNAL_STRATEGIES_RECENCY_ROUTE],
        queryParams: { strategy: state.entry.strategy_key },
      },
    ];
  });

  protected readonly tabs = computed<readonly WorkspaceTabItem[]>(() =>
    SIGNAL_STRATEGY_TABS.tabs.map((tab) => {
      const reason = UNAVAILABLE_TAB_REASONS[tab.id];
      return reason === undefined
        ? { ...tab, route: signalStrategyTabRoute(this.store.key(), tab.id) }
        : { ...tab, route: null, unavailableReason: reason };
    }),
  );

  /** Tabs the place declares but nothing serves yet, with why, where the reader can see it. */
  protected readonly pendingTabs = computed(() =>
    SIGNAL_STRATEGY_TABS.tabs.flatMap((tab) => {
      const reason = UNAVAILABLE_TAB_REASONS[tab.id];
      return reason === undefined ? [] : [{ id: tab.id, label: tab.label, reason }];
    }),
  );

  /** What the place says in the page's body when it has no strategy to show. */
  protected readonly notice = computed<WorkspaceStateContent>(() => {
    const state = this.store.entry();
    const toList: WorkspaceAction = { id: 'list', label: 'Back to Signal strategies', route: [SIGNAL_STRATEGIES_ROUTE] };
    switch (state.kind) {
      case 'failed':
        return {
          kind: 'failed',
          happened: 'The strategy registry could not be read.',
          why: state.message,
          next: `Whether "${this.store.key()}" is registered cannot be checked until it can.`,
          actions: [{ id: 'retry', label: 'Read it again' }, toList],
        };
      case 'unknown':
        return {
          kind: 'unavailable',
          happened: `No registered strategy has the key "${this.store.key()}".`,
          why: `The registry lists ${state.registered} ${state.registered === 1 ? 'strategy' : 'strategies'}, and none has this key.`,
          next: 'Choose a strategy from the list.',
          actions: [toList],
        };
      default:
        return { kind: 'checking', happened: 'Reading the strategy registry.' };
    }
  });

  protected onAction(actionId: string): void {
    if (actionId === 'retry') this.store.retry('registry');
  }
}
