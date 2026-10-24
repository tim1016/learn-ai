import { computed, inject, Injectable, resource } from '@angular/core';

import { OptionsSetupBotsService, type OptionsBotsRead } from './options-setup-bots.service';

/**
 * The bots on the fleet that were started from a setup's content, read once for the workspace and kept while the
 * owner moves between its tabs. Reading them walks the fleet: the directory, then the catalog of every ready lane,
 * so Overview asks for it once and not on every visit. It starts nothing.
 */
@Injectable()
export class OptionsSetupBotsStore {
  private readonly service = inject(OptionsSetupBotsService);
  private readonly read = resource({ loader: () => this.service.read() });

  /** What the fleet said, once it is read; null while it is being read or when the read itself failed. */
  readonly bots = computed<OptionsBotsRead | null>(() => (this.read.hasValue() ? this.read.value() : null));
  readonly failed = computed(() => this.read.error() !== undefined);

  reload(): void {
    this.read.reload();
  }
}
