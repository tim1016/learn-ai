import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, RouterOutlet } from '@angular/router';
import { map } from 'rxjs/operators';

import { WorkspaceTabListComponent, type WorkspaceTabItem } from '../../shared/workspace/workspace-tab-list.component';
import { CurrentUrlService } from '../../shell/current-url.service';
import { instrumentCommands } from '../instrument-address';
import { InstrumentContextService } from '../instrument-context.service';
import { optionsViewOf } from './options-address';
import { OPTIONS_VIEWS } from './options-views';

/**
 * An instrument's Options tab: its views side by side, one open at a time.
 *
 * Each view keeps its own inputs and its own provenance; this shell holds
 * none of them, only the strip that moves between views and the address they
 * share. A link in the strip keeps the instrument's context and the expiry the
 * address names, so the contract is the same one under every view.
 */
@Component({
  selector: 'app-options-tab',
  imports: [RouterOutlet, WorkspaceTabListComponent],
  template: `
    <app-workspace-tab-list label="Option views" [tabs]="views()" [activeId]="openView()" />
    <router-outlet />
  `,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class OptionsTabComponent {
  private readonly context = inject(InstrumentContextService);
  private readonly url = inject(CurrentUrlService).url;
  private readonly route = inject(ActivatedRoute);

  private readonly expiry = toSignal(this.route.queryParamMap.pipe(map((query) => query.get('expiry'))), {
    initialValue: this.route.snapshot.queryParamMap.get('expiry'),
  });

  protected readonly openView = computed(() => optionsViewOf(this.url()));

  protected readonly views = computed<readonly WorkspaceTabItem[]>(() => {
    const symbol = this.context.symbol();
    const expiry = this.expiry();
    const carried = { ...this.context.carriedQuery(), ...(expiry === null ? {} : { expiry }) };
    return OPTIONS_VIEWS.tabs.map((view) => ({ ...view, route: instrumentCommands(symbol, 'options', [view.id]), queryParams: carried }));
  });
}
