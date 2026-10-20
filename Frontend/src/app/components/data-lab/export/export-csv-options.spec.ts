import { describe, expect, it } from 'vitest';

import {
  columnsForPayload,
  exportTimeZoneOptions,
  toggleExportColumn,
} from './export-csv-options';

const AVAILABLE = ['open', 'high', 'low', 'close', 'volume', 'rsi_length14'];

describe('toggleExportColumn', () => {
  it('toggles a column: the first uncheck makes an explicit list, ticks edit it, a ticked column is never duplicated', () => {
    expect
      .soft(toggleExportColumn(null, AVAILABLE, 'high', false), 'turns "everything" into an explicit list on the first uncheck')
      .toEqual(['open', 'low', 'close', 'volume', 'rsi_length14']);
    expect
      .soft(toggleExportColumn(['close'], AVAILABLE, 'open', true), 'adds and removes a column from an explicit list: add')
      .toEqual(['close', 'open']);
    expect
      .soft(toggleExportColumn(['close', 'open'], AVAILABLE, 'close', false), 'adds and removes a column from an explicit list: remove')
      .toEqual(['open']);
    expect
      .soft(toggleExportColumn(['close'], AVAILABLE, 'close', true), 'never duplicates a column that is already ticked')
      .toEqual(['close']);
  });
});

describe('columnsForPayload', () => {
  it('builds the payload columns from the selection and the current plan', () => {
    expect
      .soft(columnsForPayload(null, AVAILABLE), 'sends null while nothing was edited, so later columns are included too')
      .toBeNull();
    // ema_length20 belonged to an indicator that has since been removed.
    expect
      .soft(
        columnsForPayload(['rsi_length14', 'ema_length20', 'open'], AVAILABLE),
        'sends only ticked columns the current plan still lists, in plan order',
      )
      .toEqual(['open', 'rsi_length14']);
    expect
      .soft(columnsForPayload(['close'], [...AVAILABLE, 'atr_length14']), 'leaves a new column out once the selection was edited')
      .toEqual(['close']);
    expect
      .soft(
        columnsForPayload(['close', 'open'], null),
        'passes the selection through untouched when no plan is loaded — the server validates it',
      )
      .toEqual(['close', 'open']);
  });
});

describe('exportTimeZoneOptions', () => {
  it('lists UTC first then the full IANA list with each zone once, and keeps a selected zone the runtime list does not carry', () => {
    const options = exportTimeZoneOptions('America/Chicago');
    expect.soft(options[0], 'lists UTC first, then the full IANA list, each zone once: UTC first').toBe('UTC');
    expect.soft(options, 'lists UTC first, then the full IANA list, each zone once: has America/New_York').toContain('America/New_York');
    expect.soft(options, 'lists UTC first, then the full IANA list, each zone once: has America/Chicago').toContain('America/Chicago');
    expect.soft(new Set(options).size, 'lists UTC first, then the full IANA list, each zone once: no duplicates').toBe(options.length);

    expect
      .soft(exportTimeZoneOptions('Asia/Calcutta'), 'keeps a selected zone the runtime list does not carry')
      .toContain('Asia/Calcutta');
  });
});
