import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { StrategySpec } from '../../graphql/spec-strategy.models';
import { SpecStrategyStore } from './strategy-store.service';

const SPEC: StrategySpec = {
  schema_version: '1.0',
  name: 'test',
  symbols: ['SPY'],
  resolution: { period_minutes: 15 },
  indicators: [{ id: 'sma_s', kind: 'SMA', period: 10 }],
  entry: {
    logic: 'AND',
    size: { kind: 'SetHoldings', fraction: 1 },
    conditions: [{ kind: 'FreshCross', left: 'sma_s', right: 'sma_l', direction: 'up' }],
  },
  exit: { logic: 'OR', conditions: [] },
};

describe('SpecStrategyStore', () => {
  let store: SpecStrategyStore;

  beforeEach(() => {
    localStorage.clear();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    store = TestBed.inject(SpecStrategyStore);
  });

  afterEach(() => {
    localStorage.clear();
  });

  it('starts with an empty list when storage is fresh', () => {
    expect(store.entries()).toEqual([]);
  });

  it('save creates a new entry with id, timestamps, and the spec snapshot', () => {
    const saved = store.save('My EMA', SPEC);
    expect(saved.id).toBeTruthy();
    expect(saved.name).toBe('My EMA');
    expect(saved.spec).toEqual(SPEC);
    expect(saved.createdAt).toBeGreaterThan(0);
    expect(saved.updatedAt).toBeGreaterThan(0);
    expect(store.entries()).toHaveLength(1);
  });

  it('save with existingId overwrites in place, preserving createdAt', async () => {
    const first = store.save('My EMA', SPEC);
    // Small delay so updatedAt clearly increments.
    await new Promise((r) => setTimeout(r, 5));
    const renamedSpec = { ...SPEC, name: 'edited' };
    const second = store.save('My EMA v2', renamedSpec, first.id);
    expect(second.id).toBe(first.id);
    expect(second.createdAt).toBe(first.createdAt);
    expect(second.updatedAt).toBeGreaterThanOrEqual(first.updatedAt);
    expect(second.name).toBe('My EMA v2');
    expect(second.spec.name).toBe('edited');
    expect(store.entries()).toHaveLength(1);
  });

  it('rename updates only the name', () => {
    const s = store.save('orig', SPEC);
    store.rename(s.id, 'renamed');
    expect(store.getById(s.id)?.name).toBe('renamed');
  });

  it('remove drops the entry', () => {
    const s = store.save('delete me', SPEC);
    store.remove(s.id);
    expect(store.entries()).toHaveLength(0);
  });

  it('clone copies the spec under a new id', () => {
    const original = store.save('original', SPEC);
    const cloned = store.clone(original.id, 'cloned');
    expect(cloned).toBeDefined();
    expect(cloned?.id).not.toBe(original.id);
    expect(cloned?.name).toBe('cloned');
    expect(cloned?.spec).toEqual(SPEC);
    expect(store.entries()).toHaveLength(2);
  });

  it('persists across instances by reading the same localStorage key', () => {
    store.save('persisted', SPEC);

    // Simulate page reload by reseating the TestBed.
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    const reloaded = TestBed.inject(SpecStrategyStore);

    expect(reloaded.entries()).toHaveLength(1);
    expect(reloaded.entries()[0].name).toBe('persisted');
  });

  it('sorts entries by updatedAt descending (most recent first)', async () => {
    const a = store.save('a', SPEC);
    await new Promise((r) => setTimeout(r, 5));
    store.save('b', SPEC);
    await new Promise((r) => setTimeout(r, 5));
    store.save('a updated', SPEC, a.id);

    const ids = store.entries().map((e) => e.name);
    expect(ids[0]).toBe('a updated');
    expect(ids[1]).toBe('b');
  });

  it('survives a corrupt localStorage payload by booting empty', () => {
    localStorage.setItem('learn-ai.spec-strategy.saved.v1', '{not json');
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    const fresh = TestBed.inject(SpecStrategyStore);
    expect(fresh.entries()).toEqual([]);
  });
});
