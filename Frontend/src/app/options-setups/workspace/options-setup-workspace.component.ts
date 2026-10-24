import { ChangeDetectionStrategy, Component, computed, DestroyRef, effect, inject, input, untracked } from '@angular/core';
import { Router, RouterOutlet } from '@angular/router';

import { AssetIdentityComponent } from '../../shared/asset-identity';
import { HowThisWorksLinkComponent } from '../../shared/help/how-this-works-link.component';
import { CurrentUrlService } from '../../shell/current-url.service';
import type { BreadcrumbItem } from '../../shared/breadcrumb/breadcrumb-item';
import type { WorkspaceStateContent } from '../../shared/workspace/workspace-state';
import { WorkspaceHeaderComponent } from '../../shared/workspace/workspace-header.component';
import { WorkspaceStateBlockComponent } from '../../shared/workspace/workspace-state-block.component';
import { WorkspaceTabListComponent, type WorkspaceTabItem } from '../../shared/workspace/workspace-tab-list.component';
import {
  OPTIONS_SETUP_TAB_UNAVAILABLE,
  OPTIONS_SETUP_TABS,
  OPTIONS_SETUPS_ROOT,
  optionsSetupLocation,
  optionsSetupRoute,
  UNSAVED_DRAFT_SEGMENT,
} from '../options-setups.address';
import { describeStanding } from './draft-standing';
import { OptionsSetupExploreStore } from './explore/options-setup-explore-store';
import { OptionsSetupWorkspaceStore } from './options-setup-store';

/**
 * One options setup as a place (ADR 0084): its saved name in the address, its
 * tabs, and the working copy of the setup kept alive across them.
 *
 * It fails in place, with a way back, for a name the library does not hold: it
 * never opens another setup instead. The tabs it cannot offer yet stay in the
 * strip, greyed, with the reason. Define's working copy and Explore's dates and
 * backtest are kept here, so a tab switch loses neither.
 */
@Component({
  selector: 'app-options-setup-workspace',
  imports: [RouterOutlet, WorkspaceHeaderComponent, WorkspaceTabListComponent, WorkspaceStateBlockComponent, HowThisWorksLinkComponent, AssetIdentityComponent],
  providers: [OptionsSetupWorkspaceStore, OptionsSetupExploreStore],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-setup-workspace.component.html',
  styleUrl: './options-setup-workspace.component.scss',
})
export class OptionsSetupWorkspaceComponent {
  /** The `:name` of the address; the unsaved draft's segment stands for no name. */
  readonly name = input.required<string>();

  protected readonly store = inject(OptionsSetupWorkspaceStore);
  private readonly router = inject(Router);
  private readonly currentUrl = inject(CurrentUrlService).url;

  private readonly savedName = computed(() => (this.name() === UNSAVED_DRAFT_SEGMENT ? null : this.name()));
  protected readonly activeTab = computed(() => optionsSetupLocation(this.currentUrl())?.tab ?? 'overview');
  protected readonly title = computed(() => this.savedName() ?? 'New options setup');
  protected readonly entry = this.store.entry;

  /** The setup as the library lists it, for the facts that are about the saved setup and not about the draft. */
  private readonly listed = computed(() => {
    const entry = this.entry();
    return entry.kind === 'open' ? entry.item : null;
  });
  protected readonly underlying = computed(() => this.listed()?.setup?.underlying ?? null);
  protected readonly example = computed(() => this.listed()?.example === true);
  protected readonly standing = computed(() => describeStanding(this.store.standing()));

  protected readonly breadcrumb = computed<readonly BreadcrumbItem[]>(() => {
    const trail: BreadcrumbItem[] = [{ label: 'Options setups', route: OPTIONS_SETUPS_ROOT }];
    return [...trail, { label: this.title() }];
  });

  /** A tab the workspace can open has an address; one it cannot yet stays, greyed, with its reason. */
  protected readonly tabs = computed<readonly WorkspaceTabItem[]>(() => {
    const name = this.savedName();
    return OPTIONS_SETUP_TABS.tabs.map((tab) => {
      const reason = OPTIONS_SETUP_TAB_UNAVAILABLE[tab.id];
      return reason === undefined ? { ...tab, route: optionsSetupRoute(name, tab.id) } : { ...tab, route: null, unavailableReason: reason };
    });
  });

  /** What to say, in place, when the address names no setup the workspace can open. Absent: the workspace opens. */
  protected readonly failure = computed<WorkspaceStateContent | null>(() => {
    const entry = this.entry();
    const name = this.title();
    const back = [{ id: 'index', label: 'Options setups', route: [OPTIONS_SETUPS_ROOT] }] as const;
    switch (entry.kind) {
      case 'loading':
        return { kind: 'checking', happened: 'Reading the saved setups.' };
      case 'failed':
        return { kind: 'failed', happened: 'The saved setups could not be read.', why: entry.reason, next: 'Read them again, or go back to Options setups.', actions: [{ id: 'retry', label: 'Read again' }, ...back] };
      case 'unknown':
        return {
          kind: 'unavailable',
          happened: `No saved setup is named “${name}”.`,
          why: 'It may have been deleted or the address may be wrong. Runs, searches and approved versions recorded under that name stay in Options setups and open by their own ids.',
          next: 'Choose a setup from Options setups, or start a new one.',
          actions: [...back, { id: 'new', label: 'New options setup', route: optionsSetupRoute(null, 'define') }],
        };
      case 'cannot-open':
        return {
          kind: 'unavailable',
          happened: `“${name}” is saved but can no longer be opened.`,
          why: entry.reason,
          next: 'Delete it from Options setups, or build a corrected setup and save it under another name. Nothing else is changed.',
          actions: [...back],
        };
      default:
        return null;
    }
  });

  constructor() {
    effect(() => this.store.address.set(this.savedName()));
    // A save under another name, or a draft that is no longer the saved setup, moves the address with it.
    effect(() => {
      const home = this.store.home();
      if (home === null) return;
      untracked(() => {
        const at = optionsSetupLocation(this.router.url);
        if (at === null) return;
        // A draft that asked for a tab (the setup of a run, taken from Explore) goes there as a step of its own, which Back undoes; a rename stays where it is.
        const tab = home.tab ?? at.tab;
        if (at.name === home.name && tab === at.tab) return;
        const stays = home.tab === undefined;
        void this.router.navigate(optionsSetupRoute(home.name, tab), stays ? { replaceUrl: true, queryParamsHandling: 'preserve', preserveFragment: true } : {});
      });
    });
    inject(DestroyRef).onDestroy(() => this.store.stop());
  }

  protected act(id: string): void {
    if (id === 'retry') this.store.retryLibrary();
  }
}
