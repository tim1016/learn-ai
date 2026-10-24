import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { WorkspaceTabDescriptor } from './workspace-tabs';

/** A declared tab with the address the workspace has for it right now. */
export interface WorkspaceTabItem<Id extends string = string> extends WorkspaceTabDescriptor<Id> {
  /** Router commands, or `null` for a tab this workspace cannot offer at the
   * moment. An unavailable tab stays in the strip, greyed and inert, so the
   * workspace still shows what it has rather than quietly shrinking to what
   * this instance can reach. */
  readonly route: readonly string[] | null;
  /** Why the tab has no address. Read out beside the greyed tab; never drawn. */
  readonly unavailableReason?: string;
}

/**
 * A workspace's tab strip: one link per declared tab, the open one marked,
 * and a greyed, inert entry for a tab the workspace has no address for.
 *
 * Presentation only. The workspace decides which tab is open and where each
 * one leads (the account workspace derives both from its URL); this draws
 * them. The open tab's underline is `--workspace-tab-accent`, which defaults
 * to the app accent and which a workspace sets to its own frame colour.
 */
@Component({
  selector: 'app-workspace-tab-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  templateUrl: './workspace-tab-list.component.html',
  styleUrl: './workspace-tab-list.component.scss',
})
export class WorkspaceTabListComponent {
  readonly tabs = input.required<readonly WorkspaceTabItem[]>();
  /** The open tab's `id`, or `null` when none is open. */
  readonly activeId = input.required<string | null>();
  /** The strip's name, for the surrounding navigation landmark. */
  readonly label = input.required<string>();
}
