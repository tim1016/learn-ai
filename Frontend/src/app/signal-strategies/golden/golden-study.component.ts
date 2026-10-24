import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';
import { Router } from '@angular/router';

import { GoldenSearchStudyComponent } from '../../components/golden-search/golden-search-study.component';
import { GoldenSearchService } from '../../components/golden-search/golden-search.service';
import type { WorkspaceStateContent } from '../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../shared/workspace/workspace-state-block.component';
import { readStateOf } from '../read-state';
import { SignalStrategyWorkspaceStore } from '../signal-strategy-workspace.store';
import { GOLDEN_TAB_PARAM, goldenStudyRoute, goldenTabRoute } from './golden-tabs';

/**
 * Golden → one study. A study opens only under the strategy that ran it: one that
 * another strategy ran, one that does not exist and one that cannot be read are
 * each refused in words, keeping the id, and never drawn under this strategy's
 * header. The study itself is the Golden Search workbench: its five steps, its
 * guidance and commands, its immutable history and its decision, unchanged.
 */
@Component({
  selector: 'app-golden-study',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [GoldenSearchStudyComponent, WorkspaceStateBlockComponent],
  template: `
    <div class="study">
      @let state = studyState();
      @if (state.kind === 'loading') {
        <p class="note" role="status">Reading the study.</p>
      } @else if (state.kind === 'refused') {
        <app-workspace-state-block [state]="state.state" />
      } @else {
        <app-golden-search-study
          [studyId]="studyId()"
          [strategyKey]="key()"
          [step]="step()"
          [report]="view() === 'report'"
          [capabilities]="capabilities()"
          (hidden)="onHidden()"
        />
      }
    </div>
  `,
  styles: `
    :host { display: block; min-width: 0; }
    .study { padding: var(--space-2) var(--space-4) var(--space-4); }
    .note { margin: 0; color: var(--text-secondary); font-size: var(--fs-sm); }
  `,
})
export class GoldenStudyComponent {
  private readonly store = inject(SignalStrategyWorkspaceStore);
  private readonly service = inject(GoldenSearchService);
  private readonly router = inject(Router);

  readonly studyId = input.required<string>();
  readonly step = input<string | undefined>(undefined);
  /** `report` opens the presentation report in place of the workbench. */
  readonly view = input<string | undefined>(undefined);

  protected readonly key = this.store.key;

  private readonly read = resource({
    params: () => this.studyId(),
    loader: ({ params }) => this.service.get(params),
  });

  protected readonly capabilities = computed(() => {
    const state = this.store.declarations();
    return state.kind === 'ready' && state.value.golden !== null ? [state.value.golden] : [];
  });

  protected readonly studyState = computed<
    { readonly kind: 'loading' } | { readonly kind: 'refused'; readonly state: WorkspaceStateContent } | { readonly kind: 'own' }
  >(() => {
    const id = this.studyId();
    const read = readStateOf(this.read);
    if (read.kind === 'loading') return { kind: 'loading' };
    const toStudies = { id: 'studies', label: 'All studies', route: [...goldenTabRoute(this.key(), 'studies')] };
    if (read.kind === 'failed') {
      const failure: unknown = this.read.error();
      const gone = failure instanceof HttpErrorResponse && failure.status === 404;
      return {
        kind: 'refused',
        state: {
          kind: gone ? 'unavailable' : 'failed',
          happened: gone ? `Study ${id} was not found.` : `Study ${id} could not be read.`,
          why: gone ? 'The server holds no Golden Search study with that id.' : read.message,
          next: gone ? 'Choose a study from this strategy’s history.' : 'Reload to try again.',
          actions: [toStudies],
        },
      };
    }
    const owner = read.value.strategy_key;
    if (owner !== this.key()) {
      return {
        kind: 'refused',
        state: {
          kind: 'unavailable',
          happened: `Study ${id.slice(0, 8)} ran for ${owner}, not ${this.key()}.`,
          why: 'A study opens only under the strategy it ran for, so this page shows none of its evidence.',
          next: `Open it under ${owner}.`,
          actions: [{ id: 'owner', label: `Open it under ${owner}`, route: [...goldenStudyRoute(owner, id)] }, toStudies],
        },
      };
    }
    return { kind: 'own' };
  });

  /** Hiding a study returns to the History it left. */
  protected onHidden(): void {
    void this.router.navigate([...goldenTabRoute(this.key(), 'studies')], { queryParams: { [GOLDEN_TAB_PARAM]: 'history' } });
  }
}
