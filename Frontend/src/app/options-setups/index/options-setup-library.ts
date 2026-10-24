import { computed, inject, Injectable, resource } from '@angular/core';

import { extractServerMessage } from '../../components/broker/operation-error';
import { OptionsBacktestService } from '../../components/options-backtest/options-backtest.service';
import type { SavedSetup } from '../../components/options-backtest/options-backtest.types';
import { holdersOf, type ContentHolders } from './options-setup-evidence';

export type LibraryStatus = 'loading' | 'ready' | 'error';

/**
 * The saved setups, read once for the page that provides this: the library behind a setup's workspace, the Options
 * setups list and the evidence pages. Each setup carries the hash of its content, so "which names hold this
 * content" is answered here and nowhere else (PRD #3490 R20): by equal content, never by name.
 *
 * A page that changes the library (a save, a delete) reloads it here, and everything that reads the library
 * follows. Evidence is a different read, `OptionsSetupEvidenceStore`.
 */
@Injectable()
export class OptionsSetupLibrary {
  private readonly service = inject(OptionsBacktestService);
  private readonly read = resource({ loader: () => this.service.list() });

  readonly setups = computed<readonly SavedSetup[]>(() => (this.read.hasValue() ? this.read.value() : []));
  readonly status = computed<LibraryStatus>(() => {
    if (this.read.error() !== undefined) return 'error';
    return this.read.hasValue() ? 'ready' : 'loading';
  });
  readonly problem = computed(() => {
    const error: unknown = this.read.error();
    return error === undefined ? null : extractServerMessage(error, 'The saved setups could not be read.');
  });

  reload(): void {
    this.read.reload();
  }

  /** What the saved setups say of one piece of evidence's content: no name holds it, it cannot be read, or these names hold it. */
  holdersOf(evidence: { readonly setup_sha256?: string | null; readonly unreadable_reason?: string | null }): ContentHolders<SavedSetup> {
    return holdersOf(this.setups(), evidence);
  }
}
