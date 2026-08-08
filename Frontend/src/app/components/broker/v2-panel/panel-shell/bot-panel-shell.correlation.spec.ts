import { Component, signal } from '@angular/core';
import { By } from '@angular/platform-browser';
import { provideRouter } from '@angular/router';
import { fireEvent, render, screen } from '@testing-library/angular';
import { MessageService } from 'primeng/api';
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { BrokersService } from '../../../../services/brokers.service';
import {
  ACCOUNT_ID,
  EVIDENCE_BUTTON_NAME,
  EXPECTED_OBSERVATION_COUNT,
  HIDE_EVIDENCE_BUTTON_NAME,
  INITIAL_REVISION,
  PAGE_LOAD_COUNT,
  PROFILE,
  REVISION_SEQUENCE,
  STATION_BUTTON_NAME,
  STRATEGY_INSTANCE_ID,
  TRANSACTION_REF,
  UI_CORRELATION_CAMPAIGN,
  evidenceReceipt,
  panelAtRevision,
  snapshotAtRevision,
} from '../../../../../../tests/fixtures/alpaca-clerk-ui-correlation.fixture';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import type {
  BotPanelView,
  PanelAction,
  PanelActionTrigger,
} from '../lib/broker-v2-panel.types';
import { BotPanelShellComponent } from './bot-panel-shell.component';

vi.mock('lightweight-charts', () => {
  const series = { setData: vi.fn(), update: vi.fn(), applyOptions: vi.fn() };
  return {
    createChart: vi.fn().mockReturnValue({
      addSeries: vi.fn().mockReturnValue(series),
      timeScale: vi.fn().mockReturnValue({ fitContent: vi.fn() }),
      applyOptions: vi.fn(),
      remove: vi.fn(),
    }),
    createSeriesMarkers: vi.fn().mockReturnValue({ setMarkers: vi.fn() }),
    CandlestickSeries: 'CandlestickSeries',
  };
});

class StubEventSource {
  static instances: StubEventSource[] = [];
  private readonly listeners = new Map<string, EventListenerOrEventListenerObject[]>();

  constructor(readonly url: string) {
    StubEventSource.instances.push(this);
  }

  addEventListener(name: string, listener: EventListenerOrEventListenerObject): void {
    const listeners = this.listeners.get(name) ?? [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }

  emit(name: string, data = ''): void {
    const event = new MessageEvent(name, { data });
    for (const listener of this.listeners.get(name) ?? []) {
      if (typeof listener === 'function') listener(event);
      else listener.handleEvent(event);
    }
  }

  close(): void {}
}

@Component({
  imports: [BotPanelShellComponent],
  template: `
    @if (mounted()) {
      <app-bot-panel-shell
        broker="alpaca"
        [accountId]="accountId"
        [sid]="strategyInstanceId"
      />
    }
  `,
})
class BotPanelShellReloadHost {
  readonly mounted = signal(true);
  readonly accountId = ACCOUNT_ID;
  readonly strategyInstanceId = STRATEGY_INSTANCE_ID;
}

describe('BotPanelShellComponent #1413 correlation campaign', () => {
  const originalEventSource = globalThis.EventSource;

  beforeAll(() => {
    (globalThis as { EventSource?: unknown }).EventSource = StubEventSource;
  });

  beforeEach(() => {
    StubEventSource.instances = [];
  });

  afterAll(() => {
    globalThis.EventSource = originalEventSource;
  });

  it('keeps #1413 evidence controls read-only through five mounted-shell recreations and unchanged/advancing SSE revisions', async () => {
    const lifecycleRequestActionIds: string[] = [];
    const campaignService = {
      getPanelProfile: vi.fn().mockResolvedValue(PROFILE),
      getLiveSnapshot: vi.fn().mockResolvedValue(
        snapshotAtRevision(0, INITIAL_REVISION),
      ),
      liveStreamUrl: vi.fn().mockReturnValue('/api/test/live-stream'),
      getHistoryChart: vi.fn().mockResolvedValue({
        strategy_instance_id: STRATEGY_INSTANCE_ID,
        symbol: UI_CORRELATION_CAMPAIGN.symbol,
        preset: '1D',
        aggregation: '1m',
        from_ms: 1_753_800_000_000,
        to_ms: 1_753_823_400_000,
        bars: [],
        fill_markers: [],
        truncated: false,
        as_of_ms: 1_753_800_000_000,
      }),
      getCurrentRun: vi.fn().mockRejectedValue(new Error('No current run fixture.')),
      getRunHistory: vi.fn().mockResolvedValue({ runs: [], next_cursor: null }),
      getPanel: vi.fn().mockResolvedValue(panelAtRevision(INITIAL_REVISION)),
      getEvidence: vi.fn().mockResolvedValue(
        evidenceReceipt(0, 0, INITIAL_REVISION),
      ),
      runBotAction: vi.fn((...request: unknown[]) => {
        const action = request[3] as PanelAction | undefined;
        if (action !== undefined) lifecycleRequestActionIds.push(action.action_id);
        return Promise.resolve({
          action_id: action?.action_id ?? 'unexpected',
          outcome: 'success',
          receipt_id: 'receipt-unexpected',
          recorded_at_ms: 1_753_800_000_000,
          applied: true,
          revision: action?.revision ?? 0,
          concurrency_token: action?.concurrency_token ?? 'unexpected',
          message: 'Unexpected lifecycle request.',
        });
      }),
    };
    const shellActionBoundary = BotPanelShellComponent.prototype as unknown as {
      onActionRequested(trigger: PanelActionTrigger): Promise<void>;
    };
    const outputBoundarySpy = vi.spyOn(shellActionBoundary, 'onActionRequested');
    let evidenceInteractions = 0;
    let shellRecreations = 0;
    const observedSseRevisions: number[] = [];

    try {
      const { fixture } = await render(BotPanelShellReloadHost, {
        providers: [
          provideRouter([]),
          { provide: BrokerV2PanelService, useValue: campaignService },
          {
            provide: BrokersService,
            useValue: { checkSqliteRecoveryAction: vi.fn() },
          },
          { provide: MessageService, useValue: { add: vi.fn() } },
        ],
      });
      await fixture.whenStable();

      // Destroy the initial shell so each measured cycle below is an Angular
      // component recreation; this test does not claim a browser-page reload.
      fixture.componentInstance.mounted.set(false);
      fixture.detectChanges();
      await fixture.whenStable();
      campaignService.getEvidence.mockClear();
      campaignService.runBotAction.mockClear();
      outputBoundarySpy.mockClear();

      for (let recreation = 0; recreation < PAGE_LOAD_COUNT; recreation += 1) {
        fixture.componentInstance.mounted.set(true);
        fixture.detectChanges();
        await fixture.whenStable();
        shellRecreations += 1;

        fireEvent.click(screen.getByRole('tab', { name: 'Operator' }));
        await fixture.whenStable();
        fixture.detectChanges();
        expect(screen.getByRole('button', { name: /^Resume$/ })).toBeTruthy();

        const source = StubEventSource.instances.at(-1);
        if (source === undefined) throw new Error('Expected a mounted shell SSE transport.');
        const shell = fixture.debugElement.query(By.directive(BotPanelShellComponent))
          .componentInstance as unknown as { panel(): BotPanelView | null };

        for (const revision of REVISION_SEQUENCE) {
          source.emit('snapshot', JSON.stringify(snapshotAtRevision(recreation, revision)));
          fixture.detectChanges();
          observedSseRevisions.push(revision);
          expect(shell.panel()?.revision).toBe(revision);
          expect(screen.getByRole('button', { name: /^Resume$/ })).toBeTruthy();

          const station = screen.getByRole('button', { name: STATION_BUTTON_NAME });
          fireEvent.click(station);
          fireEvent.click(screen.getByRole('button', {
            name: EVIDENCE_BUTTON_NAME,
          }));
          evidenceInteractions += 1;
          await fixture.whenStable();
          fixture.detectChanges();

          expect(screen.getByRole('button', {
            name: HIDE_EVIDENCE_BUTTON_NAME,
          })).toBeTruthy();
          fireEvent.click(screen.getByRole('button', {
            name: HIDE_EVIDENCE_BUTTON_NAME,
          }));
          fireEvent.click(station);
        }

        fixture.componentInstance.mounted.set(false);
        fixture.detectChanges();
        await fixture.whenStable();
      }

      const campaignMeasurement = {
        campaign_id: UI_CORRELATION_CAMPAIGN.component_campaign_id,
        recreation_count: shellRecreations,
        interaction_count: evidenceInteractions,
        evidence_request_count: campaignService.getEvidence.mock.calls.length,
        revision_sequence: [...REVISION_SEQUENCE],
        observed_revision_count: observedSseRevisions.length,
        output_action_count: outputBoundarySpy.mock.calls.length,
        output_action_ids: outputBoundarySpy.mock.calls.map(
          ([trigger]) => trigger.action.action_id,
        ),
        lifecycle_request_count: campaignService.runBotAction.mock.calls.length,
        lifecycle_request_action_ids: lifecycleRequestActionIds,
      };
      expect(campaignMeasurement).toEqual({
        campaign_id: UI_CORRELATION_CAMPAIGN.component_campaign_id,
        recreation_count: PAGE_LOAD_COUNT,
        interaction_count: EXPECTED_OBSERVATION_COUNT,
        evidence_request_count: EXPECTED_OBSERVATION_COUNT,
        revision_sequence: [...REVISION_SEQUENCE],
        observed_revision_count: EXPECTED_OBSERVATION_COUNT,
        output_action_count: 0,
        output_action_ids: [],
        lifecycle_request_count: 0,
        lifecycle_request_action_ids: [],
      });
      expect(observedSseRevisions).toEqual(
        Array.from(
          { length: PAGE_LOAD_COUNT },
          () => [...REVISION_SEQUENCE],
        ).flat(),
      );
      expect(campaignService.getEvidence).toHaveBeenNthCalledWith(
        EXPECTED_OBSERVATION_COUNT,
        'alpaca',
        ACCOUNT_ID,
        STRATEGY_INSTANCE_ID,
        expect.objectContaining({ transactionRef: TRANSACTION_REF }),
      );
      expect(campaignService.runBotAction).not.toHaveBeenCalled();
    } finally {
      outputBoundarySpy.mockRestore();
    }
  }, 15_000);
});
