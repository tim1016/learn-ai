import { expect, test, type Locator, type Page, type TestInfo } from '@playwright/test';

import {
  ACCOUNT_ID,
  BROWSER_RELOAD_COUNT,
  BOOTSTRAP_REVISION,
  EVIDENCE_BUTTON_NAME,
  EVIDENCE_CLICK_TARGET,
  EXPECTED_OBSERVATION_COUNT,
  FINAL_REVISION,
  HIDE_EVIDENCE_BUTTON_NAME,
  HISTORICAL_SNAPSHOT_STATE,
  PAGE_LOAD_COUNT,
  PRESENTED_ACTION_ID,
  PROFILE,
  REVISION_SEQUENCE,
  STATION_BUTTON_NAME,
  STRATEGY_INSTANCE_ID,
  TRANSACTION_REF,
  UI_CORRELATION_CAMPAIGN,
  correlationReceiptRef,
  evidenceReceipt,
  snapshotAtRevision,
} from '../fixtures/alpaca-clerk-ui-correlation.fixture';

interface Deferred {
  readonly promise: Promise<void>;
  readonly resolve: () => void;
}

interface CorrelationContext {
  readonly browserEpoch: string;
  readonly pageLoad: number;
  readonly eventIndex: number;
  readonly revision: number;
}

interface CorrelationLedgerRow {
  readonly browser_epoch: string;
  readonly page_load_index: number;
  readonly event_index: number;
  readonly revision: number;
  readonly historical_snapshot_state: typeof HISTORICAL_SNAPSHOT_STATE;
  readonly presented_action_id: typeof PRESENTED_ACTION_ID;
  readonly click_target: typeof EVIDENCE_CLICK_TARGET;
  readonly evidence_request_method: 'GET';
  readonly evidence_request_path: string;
  readonly operation_reference: string;
  readonly proof_reference: string;
  readonly dispatched_lifecycle_action_id: null;
  readonly durable_lifecycle_receipt_id: null;
}

interface BrowserSseObservation {
  readonly epoch: string;
  readonly revision: number;
}

function deferred(): Deferred {
  let resolvePromise: (() => void) | undefined;
  const promise = new Promise<void>((resolve) => {
    resolvePromise = resolve;
  });
  return {
    promise,
    resolve: () => resolvePromise?.(),
  };
}

async function instrumentNativeEventSource(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const observations: BrowserSseObservation[] = [];
    Object.defineProperty(globalThis, '__alpacaClerkSseObservations', {
      configurable: true,
      value: observations,
    });
    const originalAddEventListener = EventSource.prototype.addEventListener;
    Object.defineProperty(EventSource.prototype, 'addEventListener', {
      configurable: true,
      value: function (
        this: EventSource,
        type: string,
        listener: EventListenerOrEventListenerObject,
        options?: boolean | AddEventListenerOptions,
      ): void {
        if (type !== 'snapshot') {
          Reflect.apply(originalAddEventListener, this, [type, listener, options]);
          return;
        }
        const instrumentedListener: EventListener = (event) => {
          const message = event as MessageEvent<string>;
          const payload = JSON.parse(message.data) as {
            stream_epoch: string;
            surface_version: number;
          };
          observations.push({
            epoch: payload.stream_epoch,
            revision: payload.surface_version,
          });
          if (typeof listener === 'function') listener.call(this, event);
          else listener.handleEvent(event);
        };
        Reflect.apply(originalAddEventListener, this, [type, instrumentedListener, options]);
      },
    });
  });
}

async function observeUntil<T>(
  observe: () => Promise<T>,
  accepted: (value: T) => boolean,
  timeoutMs = 5_000,
): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  let value = await observe();
  while (!accepted(value) && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 50));
    value = await observe();
  }
  return value;
}

async function observeVisible(locator: Locator): Promise<boolean> {
  return observeUntil(
    () => locator.isVisible(),
    (visible) => visible,
  );
}

async function acceptanceAssertion(
  testInfo: TestInfo,
  assertion: () => void,
): Promise<void> {
  try {
    assertion();
  } catch (error: unknown) {
    await testInfo.attach('qualification-acceptance-assertion-failed', {
      body: JSON.stringify({ kind: 'acceptance_assertion_failure' }),
      contentType: 'application/json',
    });
    throw error;
  }
}

test.describe('Alpaca Clerk #1413 browser correlation campaign', () => {
  test('keeps evidence clicks read-only across real browser reloads and native SSE revisions', async ({ page }, testInfo) => {
    test.setTimeout(UI_CORRELATION_CAMPAIGN.timeout_seconds * 1_000);
    await instrumentNativeEventSource(page);

    const sseRelease = Array.from(
      { length: PAGE_LOAD_COUNT },
      () => REVISION_SEQUENCE.map(() => deferred()),
    );
    const sseDelivered = Array.from(
      { length: PAGE_LOAD_COUNT },
      () => REVISION_SEQUENCE.map(() => deferred()),
    );
    const sseConnectionCount = Array.from({ length: PAGE_LOAD_COUNT }, () => 0);
    const correlationLedger: CorrelationLedgerRow[] = [];
    const observedSse: BrowserSseObservation[] = [];
    const lifecycleRequestActionIds: string[] = [];
    const unexpectedApiRequests: string[] = [];
    let bootstrapCount = 0;
    let activeCorrelation: CorrelationContext | null = null;

    await page.route('**/*', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname;

      if (path.endsWith(`/bots/${STRATEGY_INSTANCE_ID}/live-snapshot`)) {
        const pageLoad = bootstrapCount;
        bootstrapCount += 1;
        await route.fulfill({ json: snapshotAtRevision(pageLoad, BOOTSTRAP_REVISION) });
        return;
      }
      if (path.endsWith(`/bots/${STRATEGY_INSTANCE_ID}/live-stream`)) {
        const cursor = url.searchParams.get('cursor') ?? '';
        const match = /^browser-reload-(\d+):\d+$/.exec(cursor);
        if (match === null) {
          await route.fulfill({ status: 400, body: 'Missing versioned cursor.' });
          return;
        }
        const pageLoad = Number(match[1]);
        const eventIndex = sseConnectionCount[pageLoad] ?? 0;
        if (eventIndex >= REVISION_SEQUENCE.length) {
          await route.abort('blockedbyclient');
          return;
        }
        sseConnectionCount[pageLoad] = eventIndex + 1;
        await sseRelease[pageLoad]?.[eventIndex]?.promise;
        const revision = REVISION_SEQUENCE[eventIndex];
        const snapshot = snapshotAtRevision(pageLoad, revision);
        await route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          headers: {
            'Cache-Control': 'no-cache',
            Connection: 'keep-alive',
          },
          body: `id: ${snapshot.stream_epoch}:${revision}\nevent: snapshot\ndata: ${JSON.stringify(snapshot)}\n\n`,
        });
        sseDelivered[pageLoad]?.[eventIndex]?.resolve();
        return;
      }
      if (path.endsWith(`/bots/${STRATEGY_INSTANCE_ID}/evidence`)) {
        if (activeCorrelation === null) {
          await route.fulfill({ status: 409, body: 'No active SSE correlation.' });
          return;
        }
        const receipt = evidenceReceipt(
          activeCorrelation.pageLoad,
          activeCorrelation.eventIndex,
          activeCorrelation.revision,
        );
        const entry = receipt.entries[0];
        correlationLedger.push({
          browser_epoch: activeCorrelation.browserEpoch,
          page_load_index: activeCorrelation.pageLoad,
          event_index: activeCorrelation.eventIndex,
          revision: activeCorrelation.revision,
          historical_snapshot_state: HISTORICAL_SNAPSHOT_STATE,
          presented_action_id: PRESENTED_ACTION_ID,
          click_target: EVIDENCE_CLICK_TARGET,
          evidence_request_method: 'GET',
          evidence_request_path: `${url.pathname}${url.search}`,
          operation_reference: entry.operation_ref,
          proof_reference: entry.proof_reference,
          dispatched_lifecycle_action_id: null,
          durable_lifecycle_receipt_id: null,
        });
        await route.fulfill({ json: receipt });
        return;
      }
      if (path.endsWith(`/bots/${STRATEGY_INSTANCE_ID}/actions`)) {
        const body = request.postDataJSON() as { action_id?: unknown };
        lifecycleRequestActionIds.push(
          typeof body.action_id === 'string' ? body.action_id : 'MISSING_ACTION_ID',
        );
        await route.fulfill({
          status: 201,
          json: {
            action_id: body.action_id,
            outcome: 'success',
            receipt_id: 'unexpected-lifecycle-receipt',
            recorded_at_ms: 1_753_800_004_000,
            applied: true,
            revision: FINAL_REVISION,
            concurrency_token: 'unexpected',
            message: 'Unexpected lifecycle request.',
          },
        });
        return;
      }
      if (path === '/api/brokers/alpaca/panel-profile') {
        await route.fulfill({ json: PROFILE });
        return;
      }
      if (path.endsWith(`/bots/${STRATEGY_INSTANCE_ID}/runs/current`)) {
        await route.fulfill({ status: 404, json: { detail: 'No current run fixture.' } });
        return;
      }
      if (path === '/api/broker/health') {
        await route.fulfill({
          json: {
            connected: false,
            is_paper: false,
            disabled: true,
            connection_state: 'disabled',
          },
        });
        return;
      }
      if (url.hostname === 'localhost' && url.port === '5000' && path === '/graphql') {
        await route.fulfill({
          headers: { 'Access-Control-Allow-Origin': '*' },
          json: { data: { getStockSnapshot: { success: false, snapshot: null } } },
        });
        return;
      }
      if (path.startsWith('/api/')) {
        unexpectedApiRequests.push(`${request.method()} ${path}`);
        await route.fulfill({ status: 501, json: { detail: 'Unexpected E2E API request.' } });
        return;
      }
      await route.continue();
    });

    for (let pageLoad = 0; pageLoad < PAGE_LOAD_COUNT; pageLoad += 1) {
      if (pageLoad === 0) {
        await page.goto(
          `/brokers/alpaca/accounts/${ACCOUNT_ID}/bots/${STRATEGY_INSTANCE_ID}?lens=operator`,
        );
      } else {
        await page.reload();
      }
      const operatorTab = page.getByRole('tab', { name: 'Operator' });
      const operatorTabSelected = await observeUntil(
        () => operatorTab.getAttribute('aria-selected'),
        (value) => value === 'true',
      );
      await acceptanceAssertion(testInfo, () => {
        expect(operatorTabSelected).toBe('true');
      });
      const resumeButton = page.getByRole('button', { name: /^Resume$/ });
      const resumeButtonVisible = await observeVisible(resumeButton);
      await acceptanceAssertion(testInfo, () => {
        expect(resumeButtonVisible).toBe(true);
      });

      for (let eventIndex = 0; eventIndex < REVISION_SEQUENCE.length; eventIndex += 1) {
        const revision = REVISION_SEQUENCE[eventIndex];
        activeCorrelation = {
          browserEpoch: `browser-reload-${pageLoad}`,
          pageLoad,
          eventIndex,
          revision,
        };
        sseRelease[pageLoad]?.[eventIndex]?.resolve();
        await sseDelivered[pageLoad]?.[eventIndex]?.promise;

        const expectedObservationCount = eventIndex + 1;
        await page.waitForFunction(
          (count) => {
            const state = globalThis as typeof globalThis & {
              __alpacaClerkSseObservations?: BrowserSseObservation[];
            };
            return state.__alpacaClerkSseObservations?.length === count;
          },
          expectedObservationCount,
        );
        const adoptedRevision = Math.max(...REVISION_SEQUENCE.slice(0, eventIndex + 1));
        const statusVisible = await observeVisible(page.getByRole('status', {
          name: `Revision ${adoptedRevision} stopped`,
        }));
        await acceptanceAssertion(testInfo, () => {
          expect(statusVisible).toBe(true);
        });
        const revisedResumeButtonVisible = await observeVisible(
          page.getByRole('button', { name: /^Resume$/ }),
        );
        await acceptanceAssertion(testInfo, () => {
          expect(revisedResumeButtonVisible).toBe(true);
        });

        const station = page.getByRole('button', { name: STATION_BUTTON_NAME });
        await station.click();
        await page.getByRole('button', { name: EVIDENCE_BUTTON_NAME }).click();
        const receiptRef = correlationReceiptRef(pageLoad, eventIndex, revision);
        const receiptRefVisible = await observeVisible(
          page.getByText(receiptRef, { exact: true }),
        );
        await acceptanceAssertion(testInfo, () => {
          expect(receiptRefVisible).toBe(true);
        });
        await page.getByRole('button', { name: HIDE_EVIDENCE_BUTTON_NAME }).click();
        await station.click();
      }
      observedSse.push(...await page.evaluate(() => {
        const state = globalThis as typeof globalThis & {
          __alpacaClerkSseObservations?: BrowserSseObservation[];
        };
        return state.__alpacaClerkSseObservations ?? [];
      }));
    }
    const measurements = {
      campaignId: UI_CORRELATION_CAMPAIGN.campaign_id,
      pageLoadCount: PAGE_LOAD_COUNT,
      browserReloadCount: BROWSER_RELOAD_COUNT,
      interactionCount: correlationLedger.length,
      evidenceRequestCount: correlationLedger.length,
      durableReceiptCount: correlationLedger.filter(
        ({ proof_reference: proofReference }) =>
          proofReference.startsWith('durable-ui-receipt-'),
      ).length,
      nativeEventSourceConnectionCount: sseConnectionCount.reduce(
        (total, count) => total + count,
        0,
      ),
      observedRevisionCount: observedSse.length,
      lifecycleRequestCount: lifecycleRequestActionIds.length,
      lifecycleRequestActionIds,
      revisionSequence: [...REVISION_SEQUENCE],
    };
    await acceptanceAssertion(testInfo, () => {
      expect(measurements).toEqual({
        campaignId: UI_CORRELATION_CAMPAIGN.campaign_id,
        pageLoadCount: PAGE_LOAD_COUNT,
        browserReloadCount: BROWSER_RELOAD_COUNT,
        interactionCount: EXPECTED_OBSERVATION_COUNT,
        evidenceRequestCount: EXPECTED_OBSERVATION_COUNT,
        durableReceiptCount: EXPECTED_OBSERVATION_COUNT,
        nativeEventSourceConnectionCount: EXPECTED_OBSERVATION_COUNT,
        observedRevisionCount: EXPECTED_OBSERVATION_COUNT,
        lifecycleRequestCount: 0,
        lifecycleRequestActionIds: [],
        revisionSequence: [...REVISION_SEQUENCE],
      });
    });
    await acceptanceAssertion(testInfo, () => {
      expect(observedSse).toEqual(
        Array.from({ length: PAGE_LOAD_COUNT }, (_, pageLoad) =>
          REVISION_SEQUENCE.map((revision) => ({
            epoch: `browser-reload-${pageLoad}`,
            revision,
          })),
        ).flat(),
      );
    });
    await acceptanceAssertion(testInfo, () => {
      expect(correlationLedger).toEqual(
        Array.from({ length: PAGE_LOAD_COUNT }, (_, pageLoad) =>
          REVISION_SEQUENCE.map((revision, eventIndex) => {
            const receiptRef = correlationReceiptRef(pageLoad, eventIndex, revision);
            return {
              browser_epoch: `browser-reload-${pageLoad}`,
              page_load_index: pageLoad,
              event_index: eventIndex,
              revision,
              historical_snapshot_state: HISTORICAL_SNAPSHOT_STATE,
              presented_action_id: PRESENTED_ACTION_ID,
              click_target: EVIDENCE_CLICK_TARGET,
              evidence_request_method: 'GET',
              evidence_request_path: `/api/brokers/alpaca/accounts/${ACCOUNT_ID}/bots/${STRATEGY_INSTANCE_ID}/evidence?transaction_ref=${TRANSACTION_REF}&page_size=12&client_hint=operator-transaction-timeline`,
              operation_reference: receiptRef,
              proof_reference: receiptRef,
              dispatched_lifecycle_action_id: null,
              durable_lifecycle_receipt_id: null,
            } satisfies CorrelationLedgerRow;
          }),
        ).flat(),
      );
    });
    await acceptanceAssertion(testInfo, () => {
      expect(lifecycleRequestActionIds).toEqual([]);
    });
    await acceptanceAssertion(testInfo, () => {
      expect(unexpectedApiRequests).toEqual([]);
    });
    await testInfo.attach('alpaca-clerk-ui-correlation-ledger.json', {
      body: `${JSON.stringify(correlationLedger, null, 2)}\n`,
      contentType: 'application/json',
    });
  });
});
