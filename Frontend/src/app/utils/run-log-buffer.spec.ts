import { describe, it, expect } from 'vitest';
import { RunLogBuffer, RUN_LOG_DEFAULT_CAP, pythonLevelToEntryLevel, glyphForLevel } from './run-log-buffer';

describe('RunLogBuffer', () => {
  it('appends entries up to the cap', () => {
    const buf = new RunLogBuffer(3);
    buf.append('info', 'ⓘ', 'one');
    buf.append('info', 'ⓘ', 'two');
    buf.append('info', 'ⓘ', 'three');
    expect(buf.size()).toBe(3);
    expect(buf.entries().map((e) => e.message)).toEqual(['one', 'two', 'three']);
  });

  it('drops the oldest entry when the cap is exceeded', () => {
    const buf = new RunLogBuffer(2);
    buf.append('info', 'ⓘ', 'a');
    buf.append('info', 'ⓘ', 'b');
    buf.append('info', 'ⓘ', 'c');
    expect(buf.size()).toBe(2);
    expect(buf.entries().map((e) => e.message)).toEqual(['b', 'c']);
  });

  it('default cap is 500', () => {
    expect(RUN_LOG_DEFAULT_CAP).toBe(500);
  });

  it('500-th and 501-st append behaves correctly', () => {
    const buf = new RunLogBuffer();
    for (let i = 0; i < 501; i++) {
      buf.append('info', 'ⓘ', `entry-${i}`);
    }
    expect(buf.size()).toBe(500);
    expect(buf.entries()[0].message).toBe('entry-1');
    expect(buf.entries()[499].message).toBe('entry-500');
  });

  it('clear() empties the buffer', () => {
    const buf = new RunLogBuffer();
    buf.append('info', 'ⓘ', 'one');
    buf.clear();
    expect(buf.size()).toBe(0);
  });

  it('entries get unique stable ids', () => {
    const buf = new RunLogBuffer();
    buf.append('info', 'ⓘ', 'a');
    buf.append('info', 'ⓘ', 'b');
    buf.append('info', 'ⓘ', 'c');
    const ids = buf.entries().map((e) => e.id);
    expect(new Set(ids).size).toBe(3);
  });
});

describe('pythonLevelToEntryLevel', () => {
  it('maps standard levels', () => {
    expect(pythonLevelToEntryLevel('info')).toBe('info');
    expect(pythonLevelToEntryLevel('warning')).toBe('warn');
    expect(pythonLevelToEntryLevel('warn')).toBe('warn');
    expect(pythonLevelToEntryLevel('error')).toBe('error');
    expect(pythonLevelToEntryLevel('critical')).toBe('error');
    expect(pythonLevelToEntryLevel('success')).toBe('success');
  });

  it('falls back to info for unknown / missing', () => {
    expect(pythonLevelToEntryLevel(undefined)).toBe('info');
    expect(pythonLevelToEntryLevel('debug')).toBe('info');
  });
});

describe('glyphForLevel', () => {
  it('returns distinct glyphs for the four severity tiers', () => {
    const set = new Set([
      glyphForLevel('info'),
      glyphForLevel('warn'),
      glyphForLevel('error'),
      glyphForLevel('success'),
    ]);
    expect(set.size).toBe(4);
  });
});
