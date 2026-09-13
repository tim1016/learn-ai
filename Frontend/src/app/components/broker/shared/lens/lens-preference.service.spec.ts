import { TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it } from 'vitest';

import { LensPreferenceService } from './lens-preference.service';

const CANONICAL_KEY = 'learn-ai.alpaca-desk.lens';

describe('LensPreferenceService', () => {
  beforeEach(() => localStorage.clear());

  it('writes and reads back a lens through the single canonical key', () => {
    TestBed.inject(LensPreferenceService).write('operator');

    expect(localStorage.getItem(CANONICAL_KEY)).toBe('operator');
    expect(TestBed.inject(LensPreferenceService).read()).toBe('operator');
  });

  it('returns null when nothing valid is stored', () => {
    localStorage.setItem(CANONICAL_KEY, 'nonsense');

    expect(TestBed.inject(LensPreferenceService).read()).toBeNull();
  });

  it('writes no second storage key', () => {
    TestBed.inject(LensPreferenceService).write('trader');

    expect(Object.keys(localStorage)).toEqual([CANONICAL_KEY]);
  });
});
