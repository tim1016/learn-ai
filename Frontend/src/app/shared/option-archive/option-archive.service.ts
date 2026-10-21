import { HttpClient } from '@angular/common/http';
import { computed, inject, Injectable, resource } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { components } from '../../api/broker.types';

export type OptionArchiveHealth = components['schemas']['OptionArchiveHealth'];

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
  readonly loading = computed(() => this.request.isLoading());

  retry(): void { this.request.reload(); }
}
