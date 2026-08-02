import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import type { components } from '../../../../api/broker.types';
import type {
  BotCatalogView,
  BotPanelView,
  ChartHistoryPreset,
  ChartHistoryResponse,
  ChartLiveResolution,
  ChartLiveResponse,
  EvidencePage,
  PanelAction,
  PanelActionRequest,
  PanelActionResult,
  PanelProfile,
} from './broker-v2-panel.types';

export type DeployBotBody = components['schemas']['AlpacaPaperDeployRequest'];
export type DeployBotReceipt = components['schemas']['AlpacaPaperDeployReceipt'];
export type DeployBotView = components['schemas']['AlpacaPaperDeployView'];
export type DeployBotStrategy = components['schemas']['AlpacaPaperDeployStrategy'];
export type DeployReadinessCheck = components['schemas']['AlpacaPaperDeployReadinessCheck'];
export type DeployExecutionMode = components['schemas']['AlpacaPaperExecutionMode'];
export type DeploySizingOption = components['schemas']['AlpacaPaperSizingOption'];
export type RunAdmissionDecision = components['schemas']['RunAdmissionDecision'];

/**
 * HTTP client for the broker-v2 panel surface.
 *
 * Targets `/api/brokers/{broker}/accounts/{accountId}/...` (the account-scoped
 * endpoints from S1). The base URL is parameterised so S4 operator lens reuses
 * this service without change.
 */
@Injectable({ providedIn: 'root' })
export class BrokerV2PanelService {
  private readonly http = inject(HttpClient);

  private base(broker: string, accountId: string): string {
    return `/api/brokers/${encodeURIComponent(broker)}/accounts/${encodeURIComponent(accountId)}`;
  }

  getPanelProfile(broker: string): Promise<PanelProfile> {
    return firstValueFrom(
      this.http.get<PanelProfile>(`/api/brokers/${encodeURIComponent(broker)}/panel-profile`),
    );
  }

  deployBot(
    broker: string,
    accountId: string,
    body: DeployBotBody,
  ): Promise<DeployBotReceipt> {
    return firstValueFrom(
      this.http.post<DeployBotReceipt>(
        `${this.base(broker, accountId)}/bots`,
        body,
      ),
    );
  }

  previewStartAdmission(
    broker: string,
    accountId: string,
    body: DeployBotBody,
  ): Promise<RunAdmissionDecision> {
    return firstValueFrom(
      this.http.post<RunAdmissionDecision>(
        `${this.base(broker, accountId)}/bots/admission`,
        body,
      ),
    );
  }

  getDeployView(
    broker: string,
    accountId: string,
  ): Promise<DeployBotView> {
    return firstValueFrom(
      this.http.get<DeployBotView>(
        `${this.base(broker, accountId)}/bots/deploy`,
      ),
    );
  }

  getCatalog(broker: string, accountId: string): Promise<BotCatalogView[]> {
    return firstValueFrom(
      this.http.get<BotCatalogView[]>(`${this.base(broker, accountId)}/bots/catalog`),
    );
  }

  getPanel(
    broker: string,
    accountId: string,
    sid: string,
    transactionRef?: string,
  ): Promise<BotPanelView> {
    let params = new HttpParams();
    if (transactionRef) {
      params = params.set('transaction_ref', transactionRef);
    }
    return firstValueFrom(
      this.http.get<BotPanelView>(
        `${this.base(broker, accountId)}/bots/${encodeURIComponent(sid)}/panel`,
        { params },
      ),
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
        `${this.base(broker, accountId)}/bots/${encodeURIComponent(sid)}/actions`,
        request,
      ),
    );
  }

  runBotAction(
    broker: string,
    accountId: string,
    sid: string,
    action: PanelAction,
  ): Promise<PanelActionResult> {
    const request: PanelActionRequest = {
      action_id: action.action_id,
      revision: action.revision,
      concurrency_token: action.concurrency_token,
      idempotency_key: crypto.randomUUID(),
      reason: null,
    };
    return this.runAction(broker, accountId, sid, request);
  }

  getLiveChart(
    broker: string,
    accountId: string,
    sid: string,
    resolution: ChartLiveResolution,
  ): Promise<ChartLiveResponse> {
    const params = new HttpParams().set('resolution', resolution);
    return firstValueFrom(
      this.http.get<ChartLiveResponse>(
        `${this.base(broker, accountId)}/bots/${encodeURIComponent(sid)}/chart/live`,
        { params },
      ),
    );
  }

  getHistoryChart(
    broker: string,
    accountId: string,
    sid: string,
    preset: ChartHistoryPreset,
  ): Promise<ChartHistoryResponse> {
    const params = new HttpParams().set('preset', preset);
    return firstValueFrom(
      this.http.get<ChartHistoryResponse>(
        `${this.base(broker, accountId)}/bots/${encodeURIComponent(sid)}/chart/history`,
        { params },
      ),
    );
  }

  /** §14 Operator-gated raw evidence — bounded, paged, audit-logged. */
  getEvidence(
    broker: string,
    accountId: string,
    sid: string,
    options: {
      transactionRef?: string;
      cursor?: number;
      pageSize?: number;
      clientHint?: string;
    } = {},
  ): Promise<EvidencePage> {
    let params = new HttpParams();
    if (options.transactionRef) params = params.set('transaction_ref', options.transactionRef);
    if (options.cursor !== undefined) params = params.set('cursor', String(options.cursor));
    if (options.pageSize !== undefined) params = params.set('page_size', String(options.pageSize));
    if (options.clientHint) params = params.set('client_hint', options.clientHint);
    return firstValueFrom(
      this.http.get<EvidencePage>(
        `${this.base(broker, accountId)}/bots/${encodeURIComponent(sid)}/evidence`,
        { params },
      ),
    );
  }
}
