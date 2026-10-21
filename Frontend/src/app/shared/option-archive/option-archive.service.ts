import { HttpClient } from '@angular/common/http';
import { computed, inject, Injectable, resource } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { components } from '../../api/broker.types';

export type OptionArchiveHealth = components['schemas']['OptionArchiveHealth'];
type ArchiveSeverity = components['schemas']['ArchiveJobHealth']['severity'];

/** The severities the server counts toward `attention`. */
const ATTENTION: ReadonlySet<ArchiveSeverity> = new Set<ArchiveSeverity>(['at_risk', 'failed', 'unavailable']);

export function needsAttention(severity: ArchiveSeverity): boolean {
  return ATTENTION.has(severity);
}

@Injectable({ providedIn: 'root' })
export class OptionArchiveService {
  private readonly http = inject(HttpClient, { optional: true });
  private readonly request = resource({
    loader: async () => {
      if (this.http === null) throw new Error('Archive status transport is unavailable.');
      return firstValueFrom(this.http.get<OptionArchiveHealth>('/api/option-archive/status'));
    },
  });
  readonly health = computed(() => this.request.hasValue() ? this.request.value() : null);
  readonly unavailable = computed(() => this.request.error() !== undefined);

  retry(): void { this.request.reload(); }
}
