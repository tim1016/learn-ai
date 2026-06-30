// PRD #617 — suggested-action renderer specs.

import { describe, expect, it, vi } from 'vitest';

import { renderSuggestedAction, type RendererDispatch } from './suggested-action-renderer';

function makeDispatch(): RendererDispatch & {
  calls: {
    invokeCapability: string[];
    focus: [string, string][];
    redeploy: number;
    openRunbook: string[];
    invokeEndpoint: string[];
  };
} {
  const calls = {
    invokeCapability: [] as string[],
    focus: [] as [string, string][],
    redeploy: 0,
    openRunbook: [] as string[],
    invokeEndpoint: [] as string[],
  };
  return {
    invokeCapability: vi.fn((cap) => calls.invokeCapability.push(cap)),
    focus: vi.fn((tab, action) => calls.focus.push([tab, action])),
    redeploy: vi.fn(() => {
      calls.redeploy += 1;
    }),
    openRunbook: vi.fn((slug) => calls.openRunbook.push(slug)),
    invokeEndpoint: vi.fn((endpoint) => calls.invokeEndpoint.push(endpoint)),
    calls,
  };
}

describe('renderSuggestedAction', () => {
  it('returns null when action is null', () => {
    expect(renderSuggestedAction(null, makeDispatch())).toBe(null);
  });

  it('invokes capability via the dispatch handler', () => {
    const dispatch = makeDispatch();
    const rendered = renderSuggestedAction(
      { kind: 'invoke_capability', capability: 'resume' },
      dispatch,
    );
    expect(rendered?.label).toBe('Resume');
    expect(rendered?.variant).toBe('primary');
    rendered?.invoke();
    expect(dispatch.calls.invokeCapability).toEqual(['resume']);
  });

  it('focus_action routes to the canonical render site for destructive actions', () => {
    const dispatch = makeDispatch();
    const rendered = renderSuggestedAction(
      { kind: 'focus_action', tab: 'audit', action: 'mark_poisoned' },
      dispatch,
    );
    expect(rendered?.label).toBe('Mark poisoned →');
    expect(rendered?.variant).toBe('link');
    rendered?.invoke();
    expect(dispatch.calls.focus).toEqual([['audit', 'mark_poisoned']]);
  });

  it('redeploy dispatches the redeploy handler', () => {
    const dispatch = makeDispatch();
    const rendered = renderSuggestedAction({ kind: 'redeploy' }, dispatch);
    rendered?.invoke();
    expect(dispatch.calls.redeploy).toBe(1);
  });

  it('open_runbook passes the slug through', () => {
    const dispatch = makeDispatch();
    const rendered = renderSuggestedAction(
      { kind: 'open_runbook', slug: 'broker-reconnect' },
      dispatch,
    );
    rendered?.invoke();
    expect(dispatch.calls.openRunbook).toEqual(['broker-reconnect']);
  });

  it('invoke_endpoint dispatches the stable backend endpoint name', () => {
    const dispatch = makeDispatch();
    const rendered = renderSuggestedAction(
      {
        kind: 'invoke_endpoint',
        endpoint: 'reconcile_instance',
        method: 'POST',
        path_template: '/api/live-instances/{strategy_instance_id}/reconcile',
      },
      dispatch,
    );
    expect(rendered?.label).toBe('Reconcile now');
    expect(rendered?.variant).toBe('primary');
    rendered?.invoke();
    expect(dispatch.calls.invokeEndpoint).toEqual(['reconcile_instance']);
  });

  it('fails closed for invoke_endpoint when the caller does not support endpoint dispatch', () => {
    const dispatch: RendererDispatch = {
      invokeCapability: vi.fn(),
      focus: vi.fn(),
      redeploy: vi.fn(),
      openRunbook: vi.fn(),
    };
    const rendered = renderSuggestedAction(
      {
        kind: 'invoke_endpoint',
        endpoint: 'reconcile_instance',
        method: 'POST',
        path_template: '/api/live-instances/{strategy_instance_id}/reconcile',
      },
      dispatch,
    );
    expect(rendered).toBe(null);
  });

  it('returns null for no primary remediation', () => {
    expect(renderSuggestedAction({ kind: 'none', reason: 'READY' }, makeDispatch())).toBe(null);
  });

  it('returns null for an unknown kind (fail closed visibly)', () => {
    const dispatch = makeDispatch();
    const rendered = renderSuggestedAction(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { kind: 'invented_future_kind' } as any,
      dispatch,
    );
    expect(rendered).toBe(null);
  });

  it('invoke_capability pause produces the Pause label and dispatches the pause capability', () => {
    const dispatch = makeDispatch();
    const rendered = renderSuggestedAction(
      { kind: 'invoke_capability', capability: 'pause' },
      dispatch,
    );
    expect(rendered?.label).toBe('Pause');
    expect(rendered?.variant).toBe('primary');
    rendered?.invoke();
    expect(dispatch.calls.invokeCapability).toEqual(['pause']);
  });

  it('redeploy returns a link-variant Redeploy label', () => {
    const dispatch = makeDispatch();
    const rendered = renderSuggestedAction({ kind: 'redeploy' }, dispatch);
    expect(rendered?.label).toBe('Redeploy →');
    expect(rendered?.variant).toBe('link');
  });

  it('open_runbook returns a link-variant Open runbook label', () => {
    const dispatch = makeDispatch();
    const rendered = renderSuggestedAction(
      { kind: 'open_runbook', slug: 'any-runbook' },
      dispatch,
    );
    expect(rendered?.label).toBe('Open runbook →');
    expect(rendered?.variant).toBe('link');
    rendered?.invoke();
    expect(dispatch.calls.openRunbook).toEqual(['any-runbook']);
  });

  it('focus_action with flatten_and_pause routes to the correct tab and action', () => {
    const dispatch = makeDispatch();
    const rendered = renderSuggestedAction(
      { kind: 'focus_action', tab: 'status', action: 'flatten_and_pause' },
      dispatch,
    );
    expect(rendered?.label).toBe('Flatten and pause →');
    expect(rendered?.variant).toBe('link');
    rendered?.invoke();
    expect(dispatch.calls.focus).toEqual([['status', 'flatten_and_pause']]);
  });

  it('focus_action with stop routes to the correct tab and action', () => {
    const dispatch = makeDispatch();
    const rendered = renderSuggestedAction(
      { kind: 'focus_action', tab: 'activity', action: 'stop' },
      dispatch,
    );
    expect(rendered?.label).toBe('Stop instance →');
    rendered?.invoke();
    expect(dispatch.calls.focus).toEqual([['activity', 'stop']]);
  });

  it('none with any reason string returns null without calling any handler', () => {
    const dispatch = makeDispatch();
    expect(renderSuggestedAction({ kind: 'none', reason: 'ACCOUNT_FROZEN' }, dispatch)).toBe(null);
    expect(dispatch.calls.invokeCapability).toHaveLength(0);
    expect(dispatch.calls.invokeEndpoint).toHaveLength(0);
  });
});
