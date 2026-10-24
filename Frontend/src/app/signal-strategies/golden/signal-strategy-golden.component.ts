import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterOutlet } from '@angular/router';

import { WorkspaceTabListComponent, type WorkspaceTabItem } from '../../shared/workspace/workspace-tab-list.component';
import { CurrentUrlService } from '../../shell/current-url.service';
import { SignalStrategyWorkspaceStore } from '../signal-strategy-workspace.store';
import { GOLDEN_TABS, goldenLocation, goldenTabRoute, type GoldenTabId } from './golden-tabs';

/** What each page of Golden is for, and what it does not claim. */
const PURPOSE: Readonly<Record<GoldenTabId, string>> = {
  studies:
    'Plan a golden search, run it in pauses and decide. A study ends in an approved version for one symbol, or in no change. Opening a study or a plan starts nothing.',
  versions:
    'What Golden Search approved: each version covers one symbol and one exact configuration, and only a ready version is current. The default is only what Deploy offers first.',
  configuration:
    'Select a completed backtest run as the baseline run and record a reviewed decision for that exact configuration (ADR 0061). A review is neither corpus coverage nor a Golden Search version.',
};

/**
 * A strategy's Golden tab: the one place settings are approved (PRD #3490 R18).
 * It holds three homes, each for one kind of evidence, and says so beneath the
 * strip so none reads as another's approval. Approval is per symbol and per
 * exact configuration; nothing on these pages sums a strategy's symbols into
 * one verdict. Explore never approves, and its handoffs end here as an unlocked
 * plan to review.
 */
@Component({
  selector: 'app-signal-strategy-golden',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterOutlet, WorkspaceTabListComponent],
  template: `
    <section class="golden">
      <p class="golden__scope">
        Settings are approved only here, one symbol and one exact configuration at a time. Explore and Proof never approve settings.
      </p>
      <app-workspace-tab-list [tabs]="tabs()" [activeId]="activeTab()" label="Golden sections" />
      @if (purpose(); as words) {
        <p class="golden__purpose">{{ words }}</p>
      }
      <router-outlet />
    </section>
  `,
  styles: `
    :host { display: block; min-width: 0; }

    .golden { display: flex; flex-direction: column; gap: var(--space-2); min-width: 0; }

    .golden__scope,
    .golden__purpose {
      margin: 0;
      padding: 0 var(--space-4);
      color: var(--text-secondary);
      font-size: var(--fs-sm);
    }

    .golden__scope { padding-top: var(--space-2); }
    .golden__purpose { max-width: 60rem; color: var(--text-subtle); font-size: var(--fs-xs); }
  `,
})
export class SignalStrategyGoldenComponent {
  private readonly store = inject(SignalStrategyWorkspaceStore);
  private readonly currentUrl = inject(CurrentUrlService).url;

  protected readonly activeTab = computed(() => goldenLocation(this.currentUrl())?.tab ?? null);

  protected readonly tabs = computed<readonly WorkspaceTabItem[]>(() =>
    GOLDEN_TABS.tabs.map((tab) => ({ ...tab, route: goldenTabRoute(this.store.key(), tab.id) })),
  );

  protected readonly purpose = computed(() => {
    const tab = this.activeTab();
    return tab === null ? null : PURPOSE[tab];
  });
}
