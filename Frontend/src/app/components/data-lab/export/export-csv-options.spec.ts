import { describe, expect, it } from 'vitest';

import {
  columnsForPayload,
  exportTimeZoneOptions,
  toggleExportColumn,
} from './export-csv-options';

const AVAILABLE = ['open', 'high', 'low', 'close', 'volume', 'rsi_length14'];

describe('toggleExportColumn', () => {
  it('turns "everything" into an explicit list on the first uncheck', () => {
    expect(toggleExportColumn(null, AVAILABLE, 'high', false)).toEqual([
      'open',
      'low',
      'close',
      'volume',
      'rsi_length14',
    ]);
  });

  it('adds and removes a column from an explicit list', () => {
    expect(toggleExportColumn(['close'], AVAILABLE, 'open', true)).toEqual(['close', 'open']);
    expect(toggleExportColumn(['close', 'open'], AVAILABLE, 'close', false)).toEqual(['open']);
  });

  it('never duplicates a column that is already ticked', () => {
    expect(toggleExportColumn(['close'], AVAILABLE, 'close', true)).toEqual(['close']);
  });
});

describe('columnsForPayload', () => {
  it('sends null while nothing was edited, so later columns are included too', () => {
    expect(columnsForPayload(null, AVAILABLE)).toBeNull();
  });

  it('sends only ticked columns the current plan still lists, in plan order', () => {
    // ema_length20 belonged to an indicator that has since been removed.
    expect(columnsForPayload(['rsi_length14', 'ema_length20', 'open'], AVAILABLE)).toEqual([
      'open',
      'rsi_length14',
    ]);
  });

  it('leaves a new column out once the selection was edited', () => {
    expect(columnsForPayload(['close'], [...AVAILABLE, 'atr_length14'])).toEqual(['close']);
  });

  it('passes the selection through untouched when no plan is loaded — the server validates it', () => {
    expect(columnsForPayload(['close', 'open'], null)).toEqual(['close', 'open']);
  });
});

describe('exportTimeZoneOptions', () => {
  it('lists UTC first, then the full IANA list, each zone once', () => {
    const options = exportTimeZoneOptions('America/Chicago');
    expect(options[0]).toBe('UTC');
    expect(options).toContain('America/New_York');
    expect(options).toContain('America/Chicago');
    expect(new Set(options).size).toBe(options.length);
  });

  it('keeps a selected zone the runtime list does not carry', () => {
    expect(exportTimeZoneOptions('Asia/Calcutta')).toContain('Asia/Calcutta');
  });
});
