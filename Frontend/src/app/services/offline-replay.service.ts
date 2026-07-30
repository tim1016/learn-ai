import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type {
  OfflineReplayCatalogResponse,
  OfflineReplayCommandRequest,
  OfflineReplayCreateRequest,
  OfflineReplaySession,
  OfflineReplaySessionListResponse,
} from '../api/offline-replay.types';

@Injectable({ providedIn: 'root' })
export class OfflineReplayService {
  private readonly http = inject(HttpClient);
  private readonly base = '/api/offline-replay';

  getCatalog(): Promise<OfflineReplayCatalogResponse> {
    return firstValueFrom(
      this.http.get<OfflineReplayCatalogResponse>(`${this.base}/catalog`),
    );
  }

  listSessions(): Promise<OfflineReplaySession[]> {
    return firstValueFrom(
      this.http.get<OfflineReplaySessionListResponse>(`${this.base}/sessions`),
    ).then((response) => response.sessions);
  }

  createSession(request: OfflineReplayCreateRequest): Promise<OfflineReplaySession> {
    return firstValueFrom(
      this.http.post<OfflineReplaySession>(`${this.base}/sessions`, request),
    );
  }

  getSession(sessionId: string): Promise<OfflineReplaySession> {
    return firstValueFrom(
      this.http.get<OfflineReplaySession>(
        `${this.base}/sessions/${encodeURIComponent(sessionId)}`,
      ),
    );
  }

  command(
    sessionId: string,
    request: OfflineReplayCommandRequest,
  ): Promise<OfflineReplaySession> {
    return firstValueFrom(
      this.http.post<OfflineReplaySession>(
        `${this.base}/sessions/${encodeURIComponent(sessionId)}/commands`,
        request,
      ),
    );
  }
}
