import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';

import { WorkspaceHeaderComponent } from '../shared/workspace/workspace-header.component';
import { WorkspaceStateBlockComponent } from '../shared/workspace/workspace-state-block.component';
import type { WorkspaceStateContent } from '../shared/workspace/workspace-state';
import { APP_MENU, HOME_ROUTE, NOT_FOUND_TITLE } from './app-menu';
import { CurrentUrlService } from './current-url.service';

/**
 * Where an unknown address ends (PRD #3490 R01): in place, at the address that
 * was typed, so Back and a corrected URL behave as they would anywhere else.
 * It never opens a default page instead; it says no page is there and leads
 * back to the app's landing, with the menu above.
 */
@Component({
  selector: 'app-not-found',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [WorkspaceHeaderComponent, WorkspaceStateBlockComponent],
  template: `
    <app-workspace-header [title]="title" />
    <app-workspace-state-block [state]="state()" />
  `,
})
export class NotFoundComponent {
  private readonly currentUrl = inject(CurrentUrlService).url;

  protected readonly title = NOT_FOUND_TITLE;

  protected readonly state = computed<WorkspaceStateContent>(() => ({
    kind: 'unavailable',
    happened: `No page is at ${this.currentUrl().split(/[?#]/)[0]}.`,
    next: `Choose a page from the menu, or go back to ${APP_MENU[0].title}.`,
    actions: [{ id: 'home', label: `Go to ${APP_MENU[0].title}`, route: [HOME_ROUTE] }],
  }));
}
