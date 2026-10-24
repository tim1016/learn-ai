import { computed, inject, Injectable, resource } from '@angular/core';

import { extractServerMessage } from '../../components/broker/operation-error';
import { cutNote, evidenceHolding, type EvidenceOfContent } from './options-setup-evidence';
import { OptionsSetupIndexService, type OptionsSetupIndex } from './options-setup-index.service';
import { OptionsSetupLibrary } from './options-setup-library';

/** The evidence read: being read, could not be, or here. */
export type EvidenceRead =
  | { readonly kind: 'loading' }
  | { readonly kind: 'failed'; readonly reason: string }
  | { readonly kind: 'ready'; readonly index: OptionsSetupIndex };

/**
 * The one read of Options evidence a place makes: every run, search and approved version, each with the content it
 * froze (PRD #3490 R20). A setup's workspace provides one beside its working copy, so Overview, Golden, Deploy and
 * Explore read it and a tab switch asks the server for nothing; the Options setups list provides its own.
 *
 * It reads again only when something changed what it holds, and the page that changed it says so: a backtest ended
 * or was deleted (`reloadIndex`), a version was approved, revoked or re-proved (`reloadIndex`), or the owner asked to
 * read again (`reload`, which reads the saved setups again as well). A save or a delete changes the saved setups and
 * no evidence, so those reload the library (`OptionsSetupLibrary.reload`) and nothing else.
 */
@Injectable()
export class OptionsSetupEvidenceStore {
  private readonly service = inject(OptionsSetupIndexService);
  private readonly library = inject(OptionsSetupLibrary);
  private readonly read = resource({ loader: () => this.service.read() });

  readonly state = computed<EvidenceRead>(() => {
    if (this.read.error() !== undefined) return { kind: 'failed', reason: extractServerMessage(this.read.error(), 'The research store did not answer.') };
    return this.read.hasValue() ? { kind: 'ready', index: this.read.value() } : { kind: 'loading' };
  });
  /** The evidence once it is read; null while it is being read or when it could not be. */
  readonly index = computed<OptionsSetupIndex | null>(() => (this.read.hasValue() ? this.read.value() : null));

  /** Said where a list was cut at its limit, so "none" is never read as "none in the newest N". */
  readonly cuts = computed(() => {
    const index = this.index();
    return index === null
      ? { runs: null, studies: null, approvals: null }
      : {
          runs: cutNote(index.runs.length, index.runs_total),
          studies: cutNote(index.studies.length, index.studies_total),
          approvals: cutNote(index.approvals.length, index.approvals_total),
        };
  });

  /** Everything that froze exactly this content; nothing, while the evidence is not read. */
  evidenceHolding(hash: string | null | undefined): EvidenceOfContent {
    const index = this.index();
    return index === null ? { runs: [], studies: [], approvals: [] } : evidenceHolding(index, hash);
  }

  /** The owner asked to read again, or a failed read is retried: the evidence and the saved setups. */
  reload(): void {
    this.read.reload();
    this.library.reload();
  }

  /** Something changed the evidence itself: a run ended or was deleted, a version was approved, revoked or re-proved. */
  reloadIndex(): void {
    this.read.reload();
  }
}
