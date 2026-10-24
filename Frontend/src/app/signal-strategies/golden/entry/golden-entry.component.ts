import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { WorkspaceAction } from '../../../shared/workspace/workspace-action';
import { WorkspaceHeaderComponent, type WorkspaceBreadcrumbItem } from '../../../shared/workspace/workspace-header.component';
import type { WorkspaceStateContent } from '../../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../../shared/workspace/workspace-state-block.component';
import { SignalStrategyChipComponent } from '../../signal-strategy-chip.component';
import { SIGNAL_STRATEGIES_ROUTE } from '../../signal-strategy-tabs';
import { goldenStudyRoute, goldenTabRoute, proofRoute } from '../golden-tabs';
import type { GoldenEntry, GoldenEntryRefusal, GoldenEntryTarget } from './golden-entry';

const TO_LIST: WorkspaceAction = { id: 'list', label: 'Back to Signal strategies', route: [SIGNAL_STRATEGIES_ROUTE] };

const TITLES: Readonly<Record<GoldenEntryTarget, { readonly choose: string; readonly crumb: string }>> = {
  studies: { choose: 'Choose a strategy to open its Golden studies', crumb: 'Golden' },
  proof: { choose: 'Choose a strategy to see its proof', crumb: 'Proof' },
  configuration: { choose: 'Choose a strategy to review a configuration', crumb: 'Golden' },
};

function refusalState(refusal: GoldenEntryRefusal): WorkspaceStateContent {
  switch (refusal.reason) {
    case 'study-not-found':
      return {
        kind: 'unavailable',
        happened: `Study ${refusal.studyId} was not found.`,
        why: 'The server holds no Golden Search study with that id, so no strategy can be named for it.',
        next: 'Check the link, or choose a strategy and open its Golden studies.',
        actions: [TO_LIST],
      };
    case 'study-unresolved':
      return {
        kind: 'unavailable',
        happened: `Study ${refusal.studyId.slice(0, 8)} stored the strategy "${refusal.storedStrategy}", which the registry does not list.`,
        why: 'The strategy may be retired or renamed. No strategy is guessed for it, and the record is unchanged.',
        next: 'Choose a registered strategy.',
        actions: [TO_LIST],
      };
    case 'study-mismatch':
      return {
        kind: 'unavailable',
        happened: `Study ${refusal.studyId.slice(0, 8)} ran for ${refusal.owner}, but the link asked for ${refusal.claimed}.`,
        why: 'A study opens only under the strategy it ran for.',
        next: `Open it under ${refusal.owner}.`,
        actions: [{ id: 'owner', label: `Open it under ${refusal.owner}`, route: [...goldenStudyRoute(refusal.owner, refusal.studyId)] }, TO_LIST],
      };
    case 'read-failed':
      return {
        kind: 'failed',
        happened: `${refusal.subject.charAt(0).toUpperCase()}${refusal.subject.slice(1)} could not be read.`,
        why: refusal.message,
        next: 'Without it the owning strategy cannot be named. Reload to try again.',
        actions: [TO_LIST],
      };
  }
}

/**
 * Where an old Golden Search or Strategy Validation address lands when its owner
 * cannot be named: a chooser of the registered strategies when the address named
 * none, or the reason in words when the stored evidence did not resolve. It stays at
 * the address it was opened at, keeping the id, and never opens a strategy the
 * evidence did not name.
 */
@Component({
  selector: 'app-golden-entry',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, SignalStrategyChipComponent, WorkspaceHeaderComponent, WorkspaceStateBlockComponent],
  templateUrl: './golden-entry.component.html',
  styleUrl: '../../explore/entry/explore-entry.component.scss',
})
export class GoldenEntryComponent {
  /** What the route's resolver found. */
  readonly entry = input.required<GoldenEntry>();

  protected readonly title = computed(() => {
    const entry = this.entry();
    return entry.kind === 'choose' ? TITLES[entry.target].choose : TITLES[entry.target].crumb;
  });

  protected readonly breadcrumb = computed<readonly WorkspaceBreadcrumbItem[]>(() => [
    { label: 'Signal strategies', route: [SIGNAL_STRATEGIES_ROUTE] },
    { label: TITLES[this.entry().target].crumb },
  ]);

  protected readonly refusal = computed<WorkspaceStateContent | null>(() => {
    const entry = this.entry();
    return entry.kind === 'refused' ? refusalState(entry.refusal) : null;
  });

  /** The registered strategies, each with whether the page asked for is open to it. */
  protected readonly choices = computed(() => {
    const entry = this.entry();
    if (entry.kind !== 'choose') return [];
    return entry.strategies
      .filter((strategy) => strategy.catalog_visible)
      .map((strategy) => {
        const golden = strategy.capabilities.golden;
        const target = entry.target;
        return {
          key: strategy.strategy_key,
          name: strategy.display_name,
          route: target === 'proof' ? proofRoute(strategy.strategy_key) : goldenTabRoute(strategy.strategy_key, target),
          // Golden Search is the registry's to rule out; a configuration review and a proof are open to every strategy.
          closedReason: target === 'studies' && !golden.supported ? (golden.reason ?? 'The registry does not allow it.') : null,
        };
      });
  });

  protected readonly unknownStrategy = computed(() => {
    const entry = this.entry();
    return entry.kind === 'choose' ? entry.unknownStrategy : null;
  });
}
