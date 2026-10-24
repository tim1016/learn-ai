import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';

import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { WorkspaceStateContent } from '../../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../../shared/workspace/workspace-state-block.component';
import { namesHolding, newestFirst } from '../../index/options-setup-evidence';
import { OptionsSetupEvidenceStore } from '../../index/options-setup-evidence-store';
import type { IndexedApproval } from '../../index/options-setup-index.service';
import { OptionsSetupLibrary } from '../../index/options-setup-library';
import { optionsEvidenceRoute, optionsSetupRoute } from '../../options-setups.address';
import { describeStanding } from '../draft-standing';
import { OptionsSetupWorkspaceStore } from '../options-setup-store';
import { OptionsSetupBotsStore } from './options-setup-bots-store';

/** How an approved version was made, in words. */
const ORIGIN_WORDS: Readonly<Record<IndexedApproval['origin'], string>> = {
  backtest: 'Approved from a backtest run',
  search: 'Approved from a golden search',
  not_recorded: 'How it was approved is not recorded',
};

/** How many runs are listed before the rest are left to the Options setups list. */
const RUNS_SHOWN = 5;

/**
 * Overview: what is recorded for exactly the content this setup is saved as
 * (PRD #3490 R20). A saved name addresses a draft that can be edited and
 * deleted, so nothing here is found by name: a run, a search or an approved
 * version is listed because it froze this very content, and a version is never
 * said to belong to a name. Identical content saved under another name shares
 * all of it, and the page says so. The evidence, the saved setups and the fleet's bots are read once for the workspace,
 * so coming back to Overview from another tab asks the server for none of them.
 */
@Component({
  selector: 'app-options-setup-overview',
  imports: [RouterLink, TimestampDisplayComponent, AuthoredUsdPipe, ReceiptLabelPipe, WorkspaceStateBlockComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-setup-overview.component.html',
  styleUrl: './options-setup-overview.component.scss',
})
export class OptionsSetupOverviewComponent {
  private readonly store = inject(OptionsSetupWorkspaceStore);
  private readonly evidence = inject(OptionsSetupEvidenceStore);
  private readonly library = inject(OptionsSetupLibrary);
  private readonly botsStore = inject(OptionsSetupBotsStore);

  protected readonly name = this.store.address;
  protected readonly standing = computed(() => describeStanding(this.store.standing()));
  protected readonly evidenceRoute = optionsEvidenceRoute;
  protected readonly defineRoute = computed(() => optionsSetupRoute(this.name(), 'define'));
  protected readonly goldenRoute = computed(() => optionsSetupRoute(this.name(), 'golden'));

  /** The saved setup, as the library lists it. */
  private readonly saved = computed(() => {
    const entry = this.store.entry();
    return entry.kind === 'open' ? entry.item : null;
  });
  protected readonly hash = this.store.savedHash;
  protected readonly example = computed(() => this.saved()?.example === true);
  protected readonly savedAtMs = computed(() => this.saved()?.saved_at_ms ?? null);

  private readonly held = computed(() => this.evidence.evidenceHolding(this.hash()));
  protected readonly approvals = computed(() => newestFirst(this.held().approvals));
  protected readonly runs = computed(() => newestFirst(this.held().runs));
  protected readonly shownRuns = computed(() => this.runs().slice(0, RUNS_SHOWN));
  protected readonly studies = computed(() => newestFirst(this.held().studies));
  /** Other saved names whose content is identical: they hold the same evidence, and none of them owns it. */
  protected readonly sharedWith = computed(() => namesHolding(this.library.setups(), this.hash()).filter((setup) => setup.name !== this.name()));

  /** Said when a list was cut at its limit: "none" would then mean "none in the newest N". */
  protected readonly cut = this.evidence.cuts;

  /** Bots started from exactly this content, or why that cannot be said. A bot is pinned to the content it was started from. */
  protected readonly botsOfContent = computed(() => {
    const hash = this.hash();
    const read = this.botsStore.bots();
    if (read === null) return null;
    if (read.kind === 'unknown') return read;
    return { kind: 'known' as const, bots: read.bots.filter((bot) => bot.setupSha256 === hash) };
  });
  protected readonly botsFailed = this.botsStore.failed;

  protected readonly state = computed<WorkspaceStateContent | null>(() => {
    if (this.name() === null) {
      return {
        kind: 'idle',
        happened: 'This draft has no saved name, so no evidence is recorded for it here.',
        next: 'Save it under a name in Define. Evidence is then listed for exactly the content you saved.',
        actions: [{ id: 'define', label: 'Go to Define', route: this.defineRoute() }],
      };
    }
    const read = this.evidence.state();
    if (read.kind === 'failed') {
      return {
        kind: 'failed',
        happened: 'The evidence recorded for this setup could not be read.',
        why: read.reason,
        next: 'Read it again. Nothing about the setup itself is lost.',
        actions: [{ id: 'retry', label: 'Read again' }],
      };
    }
    if (read.kind === 'loading') return { kind: 'checking', happened: 'Reading the evidence recorded for this setup.' };
    // The workspace opens only a setup the library holds; its content is not in hand for the moment after a save.
    if (this.hash() === null) return { kind: 'checking', happened: 'Reading the saved setup.' };
    return null;
  });

  protected act(id: string): void {
    if (id === 'retry') {
      this.evidence.reload();
      this.botsStore.reload();
    }
  }

  protected originWords(approval: IndexedApproval): string {
    return ORIGIN_WORDS[approval.origin];
  }
}
