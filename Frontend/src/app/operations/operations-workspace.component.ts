import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterOutlet } from '@angular/router';

import { CurrentUrlService } from '../shell/current-url.service';
import { HowThisWorksLinkComponent } from '../shared/help/how-this-works-link.component';
import type { HelpDocumentRoute } from '../shared/help/help-documents';
import { WorkspaceHeaderComponent } from '../shared/workspace/workspace-header.component';
import { WorkspaceTabListComponent } from '../shared/workspace/workspace-tab-list.component';
import { OPERATIONS_TABS, operationsTabOf, operationsTabRoute, type OperationsTab } from './operations-tabs';

/** The document that explains a tab, for the tabs that have one. */
const TAB_HELP: Partial<Readonly<Record<OperationsTab, HelpDocumentRoute>>> = {
  lake: '/data-quality-docs',
};

const PURPOSE =
  'The data lake, the option archive and the backups. Opening a tab reads status and starts no backfill, backup or restore.';

/**
 * The Operations place (ADR 0084): one header over its tabs. Each tab is a
 * child route, so moving between them never re-creates this shell, and a tab
 * that starts work (the Lake's backfill) does so only from its own explicit
 * controls.
 *
 * Presentation only: it reads no status and offers no action of its own.
 */
@Component({
  selector: 'app-operations-workspace',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [HowThisWorksLinkComponent, RouterOutlet, WorkspaceHeaderComponent, WorkspaceTabListComponent],
  templateUrl: './operations-workspace.component.html',
  styleUrl: './operations-workspace.component.scss',
})
export class OperationsWorkspaceComponent {
  private readonly currentUrl = inject(CurrentUrlService).url;

  protected readonly purpose = PURPOSE;
  protected readonly tabs = OPERATIONS_TABS.map((tab) => ({ ...tab, route: operationsTabRoute(tab.id) }));
  protected readonly activeTab = computed(() => operationsTabOf(this.currentUrl()));
  protected readonly help = computed(() => {
    const tab = this.activeTab();
    return tab === null ? null : (TAB_HELP[tab] ?? null);
  });
}
