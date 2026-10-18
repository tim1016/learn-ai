import { HttpClient } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { components } from '../../../api/broker.types';

type Schemas = components['schemas'];
export type PositionPreview = Schemas['PositionPreviewResponse'];
export type PreviewRequest = Schemas['PositionPreviewRequest'];

@Injectable({ providedIn: 'root' })
export class PositionPreviewService {
  private readonly http = inject(HttpClient);

  preview(request: PreviewRequest): Promise<PositionPreview> {
    return firstValueFrom(this.http.post<PositionPreview>('/api/research/options-backtest/position-preview', request));
  }
}
