import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import { EXPLORE_RUN_PARAM, EXPLORE_SEARCH_PARAM, exploreTabRoute } from '../explore-tabs';
import type { HandoffAccount } from './explore-handoff';

/**
 * What a handoff brought from the tool before it: where from, what arrived, what
 * could not and why, and what is left to choose. It is the evidence the
 * destination's form was seeded honestly, and it says plainly that nothing has
 * started. Into Golden the destination is a plan that is not locked, and the
 * notice says that nothing has been locked or run.
 */
@Component({
  selector: 'app-explore-handoff-notice',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `
    <section class="handoff" aria-label="Started from">
      <h3 class="handoff__title">
        Started from
        @if (source(); as link) {
          <a [routerLink]="link.route" [queryParams]="link.queryParams">{{ link.label }}</a>
        }
        <span class="handoff__window">{{ handoff().lineage.from }} to {{ handoff().lineage.to }}</span>
      </h3>
      <p class="handoff__lead">
        {{ leadWords() }}
      </p>
      <dl class="handoff__carried">
        @for (item of handoff().carried; track item.field) {
          <div><dt>{{ item.field }}</dt><dd>{{ item.value }}</dd></div>
        }
      </dl>
      @if (handoff().unsupported.length > 0) {
        <h4 class="handoff__heading">Not carried over</h4>
        <ul class="handoff__gaps">
          @for (gap of handoff().unsupported; track gap.field) {
            <li><strong>{{ gap.field }}</strong> {{ gap.reason }}</li>
          }
        </ul>
      }
      @if (handoff().toChoose.length > 0) {
        <h4 class="handoff__heading">Yours to choose</h4>
        <ul class="handoff__gaps">
          @for (gap of handoff().toChoose; track gap.field) {
            <li><strong>{{ gap.field }}</strong> {{ gap.reason }}</li>
          }
        </ul>
      }
    </section>
  `,
  styles: `
    :host { display: block; min-width: 0; }
    .handoff {
      display: flex;
      flex-direction: column;
      gap: var(--space-2);
      padding: var(--space-3) var(--space-4);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--bg-surface);
      font-size: var(--fs-sm);
    }
    .handoff a { color: var(--accent-text); }
    .handoff__title { margin: 0; font-size: var(--fs-md); font-weight: var(--fw-semi); }
    .handoff__window { margin-left: var(--space-2); color: var(--text-secondary); font-size: var(--fs-xs); font-weight: var(--fw-regular); }
    .handoff__lead { margin: 0; color: var(--text-secondary); }
    .handoff__heading { margin: var(--space-1) 0 0; color: var(--text-secondary); font-size: var(--fs-xs); font-weight: var(--fw-semi); text-transform: uppercase; letter-spacing: var(--ls-caps); }
    .handoff__carried { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: var(--space-2) var(--space-4); margin: 0; }
    .handoff__carried div { min-width: 0; }
    .handoff__carried dt { color: var(--text-secondary); font-size: var(--fs-xs); }
    .handoff__carried dd { margin: 0; overflow-wrap: anywhere; }
    .handoff__gaps { margin: 0; padding-left: var(--space-4); color: var(--text-secondary); }
    .handoff__gaps strong { margin-right: var(--space-1); color: var(--text-primary); }
  `,
})
export class ExploreHandoffNoticeComponent {
  readonly handoff = input.required<HandoffAccount>();

  /** The source evidence, by its own native id, in the tool that owns it. */
  protected readonly source = computed(() => {
    const { source, strategyKey } = this.handoff().lineage;
    switch (source.kind) {
      case 'backtest':
        return { label: `backtest run #${source.runId}`, route: exploreTabRoute(strategyKey, 'backtest'), queryParams: { [EXPLORE_RUN_PARAM]: String(source.runId) } };
      case 'sweep':
        return { label: `sweep ${source.searchId.slice(0, 8)}`, route: exploreTabRoute(strategyKey, 'sweep'), queryParams: { [EXPLORE_SEARCH_PARAM]: source.searchId } };
      case 'walk-forward':
        // A walk-forward study has no address of its own: it opens from its tool's history.
        return { label: `walk-forward study ${source.studyId.slice(0, 8)}`, route: exploreTabRoute(strategyKey, 'walk-forward'), queryParams: {} };
    }
  });

  protected readonly leadWords = computed(() => {
    switch (this.handoff().destination) {
      case 'sweep':
        return 'This form is filled in from that evidence so you can review it. Nothing has started; press Launch search when it looks right.';
      case 'walk-forward':
        return 'This form is filled in from that evidence so you can review it. Nothing has started; press Launch study when it looks right.';
      case 'golden':
        return 'This plan is filled in from that evidence so you can review it. Nothing is locked or running: the plan is checked as any plan is, and locking it is your decision.';
    }
  });
}
