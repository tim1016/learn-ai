import { Injectable } from '@angular/core';

import { parseLens, type DeskLens } from './lens';

/**
 * The one canonical lens preference, shared by the routed Alpaca account desk
 * and the full bot panel. Both hosts persist through exactly this key —
 * `learn-ai.alpaca-desk.lens` — and nothing else; a second storage key would
 * give the two surfaces disagreeing memories of the same preference.
 *
 * Storage is an enhancement: a browser with localStorage disabled keeps the
 * in-memory session's choice, and reads/writes never throw.
 */
@Injectable({ providedIn: 'root' })
export class LensPreferenceService {
  private static readonly STORAGE_KEY = 'learn-ai.alpaca-desk.lens';

  read(): DeskLens | null {
    if (typeof localStorage === 'undefined') return null;
    try {
      return parseLens(localStorage.getItem(LensPreferenceService.STORAGE_KEY));
    } catch (error) {
      // Storage can be disabled without making the desk unusable.
      void error;
      return null;
    }
  }

  write(lens: DeskLens): void {
    if (typeof localStorage === 'undefined') return;
    try {
      localStorage.setItem(LensPreferenceService.STORAGE_KEY, lens);
    } catch (error) {
      // Persistence is an enhancement; keep the current session's choice.
      void error;
    }
  }
}
