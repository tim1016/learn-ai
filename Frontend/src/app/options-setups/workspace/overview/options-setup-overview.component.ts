import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import { extractServerMessage } from '../../../components/broker/operation-error';
import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { WorkspaceStateContent } from '../../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../../shared/workspace/workspace-state-block.component';
import { evidenceHolding, namesHolding, newestFirst } from '../../index/options-setup-evidence';
import { OptionsSetupIndexService, type IndexedApproval } from '../../index/options-setup-index.service';
import {
  OPTIONS_SETUP_TAB_UNAVAILABLE,
  OPTIONS_SETUP_TABS,
  optionsEvidenceRoute,
  optionsSetupRoute,
  type OptionsSetupTab,
} from '../../options-setups.address';
import { describeStanding } from '../draft-standing';
import { OptionsSetupWorkspaceStore } from '../options-setup-store';
import { OptionsSetupBotsService } from './options-setup-bots.service';

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
 * all of it, and the page says so.
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
  private readonly indexService = inject(OptionsSetupIndexService);
  private readonly botsService = inject(OptionsSetupBotsService);

  protected readonly index = resource({ loader: () => this.indexService.read() });
  protected readonly bots = resource({ loader: () => this.botsService.read() });

  protected readonly name = this.store.address;
  protected readonly standing = computed(() => describeStanding(this.store.standing()));
  protected readonly evidenceRoute = optionsEvidenceRoute;
  protected readonly unavailable = computed(() =>
    OPTIONS_SETUP_TABS.tabs.flatMap((tab) => {
      const reason = OPTIONS_SETUP_TAB_UNAVAILABLE[tab.id];
      return reason === undefined ? [] : [{ id: tab.id as OptionsSetupTab, label: tab.label, reason }];
    }),
  );
  protected readonly defineRoute = computed(() => optionsSetupRoute(this.name(), 'define'));

  /** The saved setup, as the index read it just now. */
  private readonly saved = computed(() => {
    const name = this.name();
    if (name === null || !this.index.hasValue()) return null;
    return this.index.value().setups.find((setup) => setup.name === name) ?? null;
  });
  protected readonly hash = computed(() => this.saved()?.setup_sha256 ?? null);
  protected readonly example = computed(() => this.saved()?.example === true);
  protected readonly savedAtMs = computed(() => this.saved()?.saved_at_ms ?? null);

  protected readonly held = computed(() => (this.index.hasValue() ? evidenceHolding(this.index.value(), this.hash()) : null));
  protected readonly approvals = computed(() => newestFirst(this.held()?.approvals ?? []));
  protected readonly runs = computed(() => newestFirst(this.held()?.runs ?? []));
  protected readonly shownRuns = computed(() => this.runs().slice(0, RUNS_SHOWN));
  protected readonly studies = computed(() => newestFirst(this.held()?.studies ?? []));
  /** Other saved names whose content is identical: they hold the same evidence, and none of them owns it. */
  protected readonly sharedWith = computed(() =>
    this.index.hasValue() ? namesHolding(this.index.value(), this.hash()).filter((setup) => setup.name !== this.name()) : [],
  );

  /** Said when a list was cut at its limit: "none" would then mean "none in the newest N". */
  protected readonly cut = computed(() => {
    if (!this.index.hasValue()) return { runs: null, studies: null, approvals: null };
    const index = this.index.value();
    const note = (shown: number, total: number) => (total > shown ? `the newest ${shown} of ${total}` : null);
    return {
      runs: note(index.runs.length, index.runs_total),
      studies: note(index.studies.length, index.studies_total),
      approvals: note(index.approvals.length, index.approvals_total),
    };
  });

  /** Bots started from exactly this content, or why that cannot be said. A bot is pinned to the content it was started from. */
  protected readonly botsOfContent = computed(() => {
    const hash = this.hash();
    if (!this.bots.hasValue()) return null;
    const read = this.bots.value();
    if (read.kind === 'unknown') return read;
    return { kind: 'known' as const, bots: read.bots.filter((bot) => bot.setupSha256 === hash) };
  });

  protected readonly state = computed<WorkspaceStateContent | null>(() => {
    if (this.name() === null) {
      return {
        kind: 'idle',
        happened: 'This draft has no saved name, so no evidence is recorded for it here.',
        next: 'Save it under a name in Define. Evidence is then listed for exactly the content you saved.',
        actions: [{ id: 'define', label: 'Go to Define', route: this.defineRoute() }],
      };
    }
    if (this.index.error() !== undefined) {
      return {
        kind: 'failed',
        happened: 'The evidence recorded for this setup could not be read.',
        why: extractServerMessage(this.index.error(), 'The research store did not answer.'),
        next: 'Read it again. Nothing about the setup itself is lost.',
        actions: [{ id: 'retry', label: 'Read again' }],
      };
    }
    if (this.index.isLoading() && !this.index.hasValue()) return { kind: 'checking', happened: 'Reading the evidence recorded for this setup.' };
    if (this.saved() === null) {
      return {
        kind: 'unavailable',
        happened: 'The saved setup was not in the evidence read.',
        why: 'It may have been saved or removed a moment ago.',
        next: 'Read it again.',
        actions: [{ id: 'retry', label: 'Read again' }],
      };
    }
    return null;
  });

  protected act(id: string): void {
    if (id === 'retry') {
      this.index.reload();
      this.bots.reload();
    }
  }

  protected originWords(approval: IndexedApproval): string {
    return ORIGIN_WORDS[approval.origin];
  }
}
