// PRD #617 — per-instance tab state reducer specs.

import { describe, expect, it } from 'vitest';

import {
  DEFAULT_INSTANCE_TAB_STATE,
  reduceOnInstanceFocused,
  reduceOnTabSelected,
  reduceOnVerdictObserved,
} from './instance-tab-state';

describe('reduceOnVerdictObserved', () => {
  it('records initial verdict without changing selectedTab', () => {
    const { state, transition } = reduceOnVerdictObserved(
      DEFAULT_INSTANCE_TAB_STATE,
      'READY',
      true,
    );
    expect(transition).toBe('initial');
    expect(state.selectedTab).toBe('status');
    expect(state.previousVerdict).toBe('READY');
    expect(state.attentionUnseen).toBe(false);
  });

  it('forces status tab on foreground entered-attention transition', () => {
    const prior = { ...DEFAULT_INSTANCE_TAB_STATE, selectedTab: 'activity' as const, previousVerdict: 'READY' as const };
    const { state, transition } = reduceOnVerdictObserved(prior, 'BLOCKED', true);
    expect(transition).toBe('entered-attention');
    expect(state.selectedTab).toBe('status');
    expect(state.attentionUnseen).toBe(false);
  });

  it('marks background entered-attention as unseen without changing selectedTab', () => {
    const prior = { ...DEFAULT_INSTANCE_TAB_STATE, selectedTab: 'audit' as const, previousVerdict: 'READY' as const };
    const { state } = reduceOnVerdictObserved(prior, 'BLOCKED', false);
    expect(state.selectedTab).toBe('audit');
    expect(state.attentionUnseen).toBe(true);
  });

  it('does NOT re-force status on attention-changed transitions', () => {
    const prior = { ...DEFAULT_INSTANCE_TAB_STATE, selectedTab: 'audit' as const, previousVerdict: 'BLOCKED' as const };
    const { state, transition } = reduceOnVerdictObserved(prior, 'DEGRADED', true);
    expect(transition).toBe('attention-changed');
    expect(state.selectedTab).toBe('audit');
  });

  it('leaves selectedTab alone on stable polls', () => {
    const prior = { ...DEFAULT_INSTANCE_TAB_STATE, selectedTab: 'audit' as const, previousVerdict: 'READY' as const };
    const { state, transition } = reduceOnVerdictObserved(prior, 'READY', true);
    expect(transition).toBe('stable');
    expect(state.selectedTab).toBe('audit');
  });

  it('clears attentionUnseen on recovered transition (foreground)', () => {
    // The attentionUnseen flag is meant to survive until the operator
    // explicitly views the instance; a server-side recovery does NOT
    // clear it on its own (the operator may want to inspect the
    // history).  This test pins that semantic.
    const prior = {
      ...DEFAULT_INSTANCE_TAB_STATE,
      selectedTab: 'audit' as const,
      previousVerdict: 'BLOCKED' as const,
      attentionUnseen: true,
    };
    const { state, transition } = reduceOnVerdictObserved(prior, 'READY', true);
    expect(transition).toBe('recovered');
    // attentionUnseen persists — only operator interaction clears it.
    expect(state.attentionUnseen).toBe(true);
  });
});

describe('reduceOnTabSelected', () => {
  it('records the operator choice and clears attentionUnseen', () => {
    const prior = { ...DEFAULT_INSTANCE_TAB_STATE, attentionUnseen: true };
    const next = reduceOnTabSelected(prior, 'audit');
    expect(next.selectedTab).toBe('audit');
    expect(next.attentionUnseen).toBe(false);
  });
});

describe('reduceOnInstanceFocused', () => {
  it('forces status tab once when switching to an unseen-attention non-READY instance', () => {
    const prior = {
      ...DEFAULT_INSTANCE_TAB_STATE,
      selectedTab: 'audit' as const,
      attentionUnseen: true,
    };
    const next = reduceOnInstanceFocused(prior, 'BLOCKED');
    expect(next.selectedTab).toBe('status');
    expect(next.attentionUnseen).toBe(false);
  });

  it('preserves manual selection when no unseen attention', () => {
    const prior = {
      ...DEFAULT_INSTANCE_TAB_STATE,
      selectedTab: 'audit' as const,
      attentionUnseen: false,
    };
    const next = reduceOnInstanceFocused(prior, 'BLOCKED');
    expect(next.selectedTab).toBe('audit');
  });

  it('does not force status when instance has recovered before focus', () => {
    const prior = {
      ...DEFAULT_INSTANCE_TAB_STATE,
      selectedTab: 'audit' as const,
      attentionUnseen: true,
    };
    const next = reduceOnInstanceFocused(prior, 'READY');
    expect(next.selectedTab).toBe('audit');
    expect(next.attentionUnseen).toBe(false);
  });
});
