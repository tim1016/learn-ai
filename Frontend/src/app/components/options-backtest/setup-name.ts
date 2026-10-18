import type { LegDraft, SetupDraft } from './setup-draft';

/** Predictable setup label, using only characters accepted by the saved-setup name contract. */
export function generatedSetupName(draft: SetupDraft): string {
  const symbol = draft.underlying.trim().toUpperCase();
  if (!symbol || draft.legs.length === 0) return '';
  const timing = draft.entryClock && draft.exitClock ? `, ${draft.entryClock.replace(':', '')}-${draft.exitClock.replace(':', '')} ET` : '';
  const legs = draft.legs.map(legName);
  const full = `${symbol}, ${legs.join(' and ')}${timing}`;
  if (full.length <= 80) return full;
  return `${symbol}, ${legs[0]} and ${legs.length - 1} more legs${timing}`.slice(0, 80).trimEnd();
}

function legName(leg: LegDraft): string {
  const strike = leg.strikeKind === 'atm' ? 'ATM' : `ATM ${leg.strikeSteps < 0 ? 'minus' : 'plus'} ${Math.abs(leg.strikeSteps)}`;
  return `${leg.side === 'sell' ? 'Sell' : 'Buy'} ${leg.contracts} ${strike} ${leg.right}`;
}
