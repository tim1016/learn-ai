import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type {
  BotCatalogView,
  PanelActionRequest,
  PanelActionResult,
  PanelProfile,
} from './models/broker-v2-panel.types';

@Injectable({ providedIn: 'root' })
export class BrokerV2PanelService {
  private readonly http = inject(HttpClient);
  private readonly base = '/api/brokers';

  getCatalog(broker: string, accountId?: string): Promise<BotCatalogView[]> {
    if (accountId) {
      return firstValueFrom(
        this.http.get<BotCatalogView[]>(
          `${this.base}/${broker}/accounts/${accountId}/bots/catalog`,
        ),
      );
    }
    return firstValueFrom(
      this.http.get<BotCatalogView[]>(`${this.base}/${broker}/bots/catalog`),
    );
  }

  getPanelProfile(broker: string): Promise<PanelProfile> {
    return firstValueFrom(
      this.http.get<PanelProfile>(`${this.base}/${broker}/panel-profile`),
    );
  }

  deployBot(
    broker: string,
    body: { strategy_instance_id: string; symbol: string; rth_only: boolean },
  ): Promise<unknown> {
    return firstValueFrom(
      this.http.post<unknown>(`${this.base}/${broker}/bots`, body),
    );
  }

  runAction(
    broker: string,
    accountId: string,
    sid: string,
    request: PanelActionRequest,
  ): Promise<PanelActionResult> {
    return firstValueFrom(
      this.http.post<PanelActionResult>(
        `${this.base}/${broker}/accounts/${accountId}/bots/${sid}/actions`,
        request,
      ),
    );
  }
}
