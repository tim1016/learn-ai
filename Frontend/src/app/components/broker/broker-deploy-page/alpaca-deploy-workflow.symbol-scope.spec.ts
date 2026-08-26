import { fireEvent, render, screen } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import { describe, expect, it, vi } from 'vitest';

import {
  BrokerV2PanelService,
  type DeployBotView,
} from '../v2-panel/lib/broker-v2-panel.service';
import { AlpacaDeployWorkflowComponent } from './alpaca-deploy-workflow.component';
import { DEPLOY_VIEW } from './alpaca-deploy-workflow.fixtures';

const ADMISSION_STUB = { allowed: true };

/** A second strategy whose validation case is a different symbol. */
const QQQ_STRATEGY: DeployBotView['strategies'][number] = {
  ...DEPLOY_VIEW.strategies[0],
  strategy_key: 'qqq_momentum',
  label: 'QQQ Momentum',
  validation_case_symbol: 'QQQ',
};

const QQQ_VIEW: DeployBotView = {
  ...DEPLOY_VIEW,
  strategies: [...DEPLOY_VIEW.strategies, QQQ_STRATEGY],
};

/** A view whose admission headline names it, so the DOM says which one landed. */
function labelledView(headline: string): DeployBotView {
  return {
    ...DEPLOY_VIEW,
    eligibility: { ...DEPLOY_VIEW.eligibility, headline },
  };
}

interface Deferred<T> {
  readonly promise: Promise<T>;
  resolve(value: T): void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function mockService(view: DeployBotView = DEPLOY_VIEW) {
  return {
    getDeployView: vi.fn().mockResolvedValue(view),
    previewStartAdmission: vi.fn().mockResolvedValue(ADMISSION_STUB),
    deployBot: vi.fn(),
  };
}

async function renderWorkflow(service = mockService()) {
  const rendered = await render(AlpacaDeployWorkflowComponent, {
    providers: [
      provideRouter([]),
      { provide: BrokerV2PanelService, useValue: service },
    ],
    componentInputs: { accountId: 'PA9' },
  });
  await screen.findByRole('heading', { name: 'Bot binding' });
  return rendered;
}

/**
 * Symbol scoping and readiness staleness for the Alpaca deploy workflow.
 *
 * Split out of `alpaca-deploy-workflow.component.spec.ts` when that file
 * crossed the 1,000-line rule. This half owns one seam: which symbol the
 * readiness fetch is scoped to, and what the pane may still show and still
 * admit once a refresh for that scope stops landing.
 */
describe('AlpacaDeployWorkflowComponent symbol scoping', () => {
  // ── WP3 (#1777): symbol-scoped deploy readiness ───────────────────────────
  // The GET accepts `?symbol=`, so channel health can be scoped to the symbol
  // the operator actually intends to trade. The hazard is the feedback loop:
  // the ticket symbol is SEEDED from the loaded view, so keying the resource
  // on it would re-fetch forever and strand the pane on its loading state.

  const SYMBOL_DEBOUNCE_MS = 400;

  async function typeSymbol(value: string): Promise<void> {
    fireEvent.input(screen.getByPlaceholderText('SPY'), { target: { value } });
  }

  function deployButton(): HTMLElement {
    return screen.getByRole('button', { name: /^Deploy / });
  }

  it('re-fetches the deploy view scoped to a symbol the operator picks', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const service = mockService();
      await renderWorkflow(service);
      service.getDeployView.mockClear();

      await typeSymbol('QQQ');
      await vi.advanceTimersByTimeAsync(SYMBOL_DEBOUNCE_MS + 50);

      expect(service.getDeployView).toHaveBeenCalledWith('alpaca', 'PA9', 'QQQ');
    } finally {
      vi.useRealTimers();
    }
  });

  it('scopes readiness to the seeded symbol and then settles', async () => {
    // The seed is where the symbol first reaches the ticket, so it is where
    // scoping has to start: the first load carries no symbol, and leaving it
    // there showed ACCOUNT-level channel health under a populated symbol —
    // the exact scoping this work exists to wire, inactive on load.
    //
    // What must NOT happen is a loop. The seeded scope re-fetches once; the
    // view it returns seeds the same symbol, the `scopedSymbol() === symbol`
    // guard recognizes it, and the pane settles.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const service = mockService();
      await renderWorkflow(service);
      expect((screen.getByPlaceholderText('SPY') as HTMLInputElement).value).toBe('SPY');
      service.getDeployView.mockClear();

      await vi.advanceTimersByTimeAsync(SYMBOL_DEBOUNCE_MS + 50);
      expect(service.getDeployView).toHaveBeenCalledTimes(1);
      expect(service.getDeployView).toHaveBeenCalledWith('alpaca', 'PA9', 'SPY');

      await vi.advanceTimersByTimeAsync(SYMBOL_DEBOUNCE_MS * 5);
      expect(service.getDeployView).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('rescopes readiness when a strategy switch moves the symbol', async () => {
    // A strategy switch adopts the new strategy's validation-case symbol.
    // Writing that to the ticket without re-scoping left symbol-A's gates on
    // screen under symbol B — and `canSubmit` gating on them.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const service = mockService(QQQ_VIEW);
      await renderWorkflow(service);
      await vi.advanceTimersByTimeAsync(SYMBOL_DEBOUNCE_MS + 50);
      service.getDeployView.mockClear();

      fireEvent.change(screen.getByLabelText('Deployment strategy'), {
        target: { value: QQQ_STRATEGY.strategy_key },
      });
      await vi.advanceTimersByTimeAsync(SYMBOL_DEBOUNCE_MS + 50);

      expect(screen.getByPlaceholderText<HTMLInputElement>('SPY').value).toBe('QQQ');
      expect(service.getDeployView).toHaveBeenCalledWith('alpaca', 'PA9', 'QQQ');
    } finally {
      vi.useRealTimers();
    }
  });

  it('collapses a burst of keystrokes into one scoped fetch', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const service = mockService();
      await renderWorkflow(service);
      service.getDeployView.mockClear();

      await typeSymbol('Q');
      await typeSymbol('QQ');
      await typeSymbol('QQQ');
      await vi.advanceTimersByTimeAsync(SYMBOL_DEBOUNCE_MS + 50);

      expect(service.getDeployView).toHaveBeenCalledTimes(1);
      expect(service.getDeployView).toHaveBeenCalledWith('alpaca', 'PA9', 'QQQ');
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps the readiness pane rendered while the scoped view reloads', async () => {
    // A `params` change drops the prior value and the pane flickers back to
    // "Loading deployment readiness". An explicit reload() must not.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const service = mockService();
      await renderWorkflow(service);

      await typeSymbol('QQQ');
      await vi.advanceTimersByTimeAsync(SYMBOL_DEBOUNCE_MS + 50);

      expect(screen.queryByLabelText('Loading deployment readiness')).toBeNull();
      expect(screen.getByRole('heading', { name: 'Bot binding' })).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not scope the fetch to a symbol that is not yet a valid ticker', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const service = mockService();
      await renderWorkflow(service);
      service.getDeployView.mockClear();

      await typeSymbol('!!');
      await vi.advanceTimersByTimeAsync(SYMBOL_DEBOUNCE_MS + 50);

      expect(service.getDeployView).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it('reports the server-observed evaluation time in the footer', async () => {
    await renderWorkflow();

    const footer = screen.getByTestId('deploy-footer-observed');
    // Server-authored `evaluated_at_ms`, rendered by the shared component —
    // never a client clock (temporal-rigor.md).
    expect(footer.querySelector('app-timestamp-display')).toBeTruthy();
    expect(footer.textContent).toContain('Readiness observed');
  });

  it('raises an explicit staleness banner when a scoped refresh stops landing', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const service = mockService();
      await renderWorkflow(service);
      await vi.advanceTimersByTimeAsync(SYMBOL_DEBOUNCE_MS + 50);
      service.getDeployView.mockRejectedValue(new Error('data plane unreachable'));

      await typeSymbol('QQQ');
      await vi.advanceTimersByTimeAsync(SYMBOL_DEBOUNCE_MS + 50);

      const banner = await screen.findByRole('alert', { name: 'Deployment readiness is stale' });
      expect(banner.textContent).toContain('QQQ');
    } finally {
      vi.useRealTimers();
    }
  });

  // ── The retained view's symbol identity (#1778) ───────────────────────────
  // Retaining the last loaded gates is what keeps a failed refresh from
  // throwing away the operator's whole ticket. It is only safe while the
  // retained view says WHICH symbol it describes, and while nothing may be
  // deployed on the strength of it.

  it('names the symbol whose gates are actually on screen, not just the one it wanted', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const service = mockService();
      await renderWorkflow(service);
      await vi.advanceTimersByTimeAsync(SYMBOL_DEBOUNCE_MS + 50);
      service.getDeployView.mockRejectedValue(new Error('data plane unreachable'));

      await typeSymbol('QQQ');
      await vi.advanceTimersByTimeAsync(SYMBOL_DEBOUNCE_MS + 50);

      const banner = await screen.findByRole('alert', { name: 'Deployment readiness is stale' });
      // Keyed on `{accountId, symbol}`: an unidentified retained view let a
      // SPY-scoped set of gates silently back a QQQ ticket.
      expect(banner.textContent).toContain('QQQ');
      expect(banner.textContent).toContain('SPY');
    } finally {
      vi.useRealTimers();
    }
  });

  it('refuses to admit a deploy while the gates on screen are stale', async () => {
    // A `role="alert"` staleness banner beside a live Deploy button is the
    // worst of both: the operator is told the truth and still handed the
    // means to act on superseded admission truth.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const service = mockService();
      await renderWorkflow(service);
      fireEvent.input(screen.getByLabelText('Bot name'), { target: { value: 'spy-scope-01' } });
      await vi.advanceTimersByTimeAsync(SYMBOL_DEBOUNCE_MS + 50);
      // Baseline: this ticket is otherwise deployable, so the assertion below
      // is about staleness and nothing else.
      expect(deployButton().hasAttribute('disabled')).toBe(false);

      service.getDeployView.mockRejectedValue(new Error('data plane unreachable'));
      await typeSymbol('QQQ');
      await vi.advanceTimersByTimeAsync(SYMBOL_DEBOUNCE_MS + 50);
      await screen.findByRole('alert', { name: 'Deployment readiness is stale' });

      expect(deployButton().hasAttribute('disabled')).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it('applies a scope the resource was too busy to reload for', async () => {
    // `reload()` is refused outright while a load is in flight, so a second
    // pick made during the first fetch used to update the requested scope and
    // never fetch it — stranding the pane on gates for a symbol the operator
    // had already left, with no way back but another keystroke.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const inFlight = deferred<DeployBotView>();
      const service = mockService();
      await renderWorkflow(service);
      await vi.advanceTimersByTimeAsync(SYMBOL_DEBOUNCE_MS + 50);

      service.getDeployView.mockImplementation((_broker: string, _account: string, symbol?: string) =>
        symbol === 'QQQ' ? inFlight.promise : Promise.resolve(DEPLOY_VIEW),
      );
      await typeSymbol('QQQ');
      await vi.advanceTimersByTimeAsync(SYMBOL_DEBOUNCE_MS + 50);

      await typeSymbol('IWM');
      await vi.advanceTimersByTimeAsync(SYMBOL_DEBOUNCE_MS + 50);
      inFlight.resolve(DEPLOY_VIEW);
      await vi.advanceTimersByTimeAsync(SYMBOL_DEBOUNCE_MS + 50);

      expect(service.getDeployView).toHaveBeenCalledWith('alpaca', 'PA9', 'IWM');
    } finally {
      vi.useRealTimers();
    }
  });

  it('drops a superseded response instead of letting it overwrite the retained gates', async () => {
    // `getDeployView` resolves a promise no `reload()` or params change can
    // cancel, so a slow earlier request still answers — and, being slower,
    // can answer last. If its write wins, the retained record claims an
    // account the operator has already left, and the next failed refresh
    // finds no gates to stand behind.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const pa9Scoped = deferred<DeployBotView>();
      const service = mockService();
      const { rerender } = await renderWorkflow(service);

      service.getDeployView.mockImplementation((_broker: string, accountId: string) =>
        accountId === 'PA9' ? pa9Scoped.promise : Promise.resolve(labelledView('PA7 readiness')),
      );
      await vi.advanceTimersByTimeAsync(SYMBOL_DEBOUNCE_MS + 50);

      // Switching account supersedes the in-flight PA9 request.
      await rerender({ componentInputs: { accountId: 'PA7' } });
      await vi.advanceTimersByTimeAsync(10);
      pa9Scoped.resolve(labelledView('PA9 readiness'));
      await vi.advanceTimersByTimeAsync(10);

      // Fail the next refresh so the retained record is what renders, then
      // read off which account it kept.
      service.getDeployView.mockRejectedValue(new Error('data plane unreachable'));
      await typeSymbol('DIA');
      await vi.advanceTimersByTimeAsync(SYMBOL_DEBOUNCE_MS + 50);
      await screen.findByRole('alert', { name: 'Deployment readiness is stale' });

      expect(screen.getByText('PA7 readiness')).toBeTruthy();
      expect(screen.queryByText('PA9 readiness')).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
