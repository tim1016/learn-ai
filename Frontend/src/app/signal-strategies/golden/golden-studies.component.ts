import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { Router } from '@angular/router';
import { Tab, TabList, TabPanel, TabPanels, Tabs } from 'primeng/tabs';

import { extractServerMessage } from '../../components/broker/operation-error';
import { GoldenSearchHistoryComponent } from '../../components/golden-search/golden-search-history.component';
import { GoldenSearchPlanFormComponent } from '../../components/golden-search/golden-search-plan-form.component';
import { GoldenSearchService } from '../../components/golden-search/golden-search.service';
import type { StudyDetail } from '../../components/golden-search/golden-search.types';
import type { WorkspaceStateContent } from '../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../shared/workspace/workspace-state-block.component';
import { ExploreSession } from '../explore/explore-session';
import { ExploreHandoffNoticeComponent } from '../explore/handoff/explore-handoff-notice.component';
import { SignalStrategyWorkspaceStore } from '../signal-strategy-workspace.store';
import { goldenStudyRoute } from './golden-tabs';
import { injectGoldenHandoff } from './golden-handoff';

type StudiesTab = 'new' | 'history';

/**
 * Golden → Studies (ADR 0074): plan a golden search for this strategy and list its
 * studies. `?tab=history` opens the History tab; `?revise=<id>` starts the plan
 * from that study's frozen protocol (locking it creates a new linked study); and
 * `?run=`, `?search=` or `?walkForward=` open the plan from the Explore evidence
 * they name, which only fills the plan in for review: it is unlocked, it starts no
 * stage, and the explored window is compared with the final test's dates. A locked
 * plan navigates straight to its study.
 */
@Component({
  selector: 'app-golden-studies',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ExploreHandoffNoticeComponent,
    GoldenSearchHistoryComponent,
    GoldenSearchPlanFormComponent,
    Tab,
    TabList,
    TabPanel,
    TabPanels,
    Tabs,
    WorkspaceStateBlockComponent,
  ],
  templateUrl: './golden-studies.component.html',
  styleUrl: './golden-studies.component.scss',
})
export class GoldenStudiesComponent {
  private readonly store = inject(SignalStrategyWorkspaceStore);
  private readonly session = inject(ExploreSession);
  private readonly service = inject(GoldenSearchService);
  private readonly router = inject(Router);

  /** `history` opens the History tab. */
  readonly tab = input<string | undefined>(undefined);
  /** The study whose frozen plan a new study revises. */
  readonly revise = input<string | undefined>(undefined);
  /** The Explore evidence a plan is opened from, by its own native id. */
  readonly run = input<string | undefined>(undefined);
  readonly search = input<string | undefined>(undefined);
  readonly walkForward = input<string | undefined>(undefined);

  protected readonly key = this.store.key;
  readonly activeTab = signal<StudiesTab>('new');
  readonly reviseFrom = signal<StudyDetail | null>(null);
  readonly reviseError = signal<string | null>(null);

  /** This strategy's Golden Search declaration, once read; `null` when there is none or it is not read yet. */
  protected readonly declaration = computed(() => {
    const state = this.store.declarations();
    return state.kind === 'ready' ? state.value.golden : null;
  });
  protected readonly capabilities = computed(() => {
    const declaration = this.declaration();
    return declaration === null ? [] : [declaration];
  });

  protected readonly handoff = injectGoldenHandoff(this.run, this.search, this.walkForward, this.declaration);
  protected readonly readyHandoff = computed(() => {
    const state = this.handoff();
    return state?.kind === 'ready' ? state.handoff : null;
  });

  /** Why the page cannot offer a plan at all for this strategy, or `null`. */
  protected readonly blocked = computed<WorkspaceStateContent | null>(() => {
    const entry = this.store.entry();
    if (entry.kind === 'found' && !entry.entry.capabilities.golden.supported) {
      return {
        kind: 'unavailable',
        happened: 'The registry does not allow a golden search of this strategy.',
        why: entry.entry.capabilities.golden.reason,
        next: 'Backtest it in Explore, or choose a strategy the registry allows.',
      };
    }
    const declarations = this.store.declarations();
    if (declarations.kind === 'loading') return { kind: 'checking', happened: "Reading the strategy's Golden Search declaration." };
    if (declarations.kind === 'failed') {
      return {
        kind: 'failed',
        happened: "The strategy's Golden Search declaration could not be read.",
        why: declarations.message,
        next: 'A plan needs its search ranges, steps and constraints.',
        actions: [{ id: 'retry', label: 'Read it again' }],
      };
    }
    if (declarations.value.golden === null) {
      return {
        kind: 'unavailable',
        happened: `Golden Search declares nothing for ${this.key()}.`,
        why: 'Its search ranges, steps and constraints come from that declaration, so there is no plan to build.',
        next: 'Choose a strategy that declares one.',
      };
    }
    return null;
  });

  /** Generation of the latest revise request; an older study must not replace a newer one. */
  private reviseGeneration = 0;

  constructor() {
    this.session.wantCatalog();
    effect(() => {
      if (this.tab() === 'history') untracked(() => this.activeTab.set('history'));
    });
    effect(() => {
      const id = this.revise();
      untracked(() => void this.loadRevision(id));
    });
  }

  protected onAction(actionId: string): void {
    if (actionId === 'retry') this.store.retry('declarations');
  }

  protected setTab(tab: string | number | undefined): void {
    if (tab === 'new' || tab === 'history') this.activeTab.set(tab);
  }

  protected onLocked(studyId: string): void {
    void this.router.navigate([...goldenStudyRoute(this.key(), studyId)]);
  }

  private async loadRevision(id: string | undefined): Promise<void> {
    const generation = ++this.reviseGeneration;
    this.reviseFrom.set(null);
    this.reviseError.set(null);
    if (!id) return;
    this.activeTab.set('new');
    try {
      const study = await this.service.get(id);
      if (generation !== this.reviseGeneration) return;
      // A study is revised only under the strategy that ran it; another's plan never fills this strategy's form.
      if (study.strategy_key !== this.key()) {
        this.reviseError.set(`Study ${id.slice(0, 8)} belongs to ${study.strategy_key}, not ${this.key()}. The plan starts from the defaults instead.`);
        return;
      }
      this.reviseFrom.set(study);
    } catch (error) {
      if (generation === this.reviseGeneration) {
        this.reviseError.set(`${extractServerMessage(error, 'The study to revise could not be loaded.')} The plan starts from the defaults instead.`);
      }
    }
  }
}
