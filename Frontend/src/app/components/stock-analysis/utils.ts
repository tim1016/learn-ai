import { OptionsContract } from '../../graphql/types';

/**
 * Select ATM + N ITM + N OTM contracts for both calls and puts.
 * For calls: ITM = strikes below ATM, OTM = strikes above ATM.
 * For puts:  ITM = strikes above ATM, OTM = strikes below ATM.
 */
export function selectNearAtmContracts(
  contracts: OptionsContract[],
  atmPrice: number,
  numItm: number,
  numOtm: number
): OptionsContract[] {
  const selected: OptionsContract[] = [];

  for (const type of ['call', 'put'] as const) {
    const typed = contracts
      .filter(c => c.contractType === type && c.strikePrice != null)
      .sort((a, b) => a.strikePrice! - b.strikePrice!);

    // Deduplicate by strike price (keep first expiration encountered)
    const byStrike = new Map<number, OptionsContract>();
    for (const c of typed) {
      if (!byStrike.has(c.strikePrice!)) {
        byStrike.set(c.strikePrice!, c);
      }
    }

    const unique = [...byStrike.values()].sort((a, b) => a.strikePrice! - b.strikePrice!);
    if (unique.length === 0) continue;

    // Find ATM: closest strike to atmPrice
    let atmIdx = 0;
    let minDiff = Math.abs(unique[0].strikePrice! - atmPrice);
    for (let i = 1; i < unique.length; i++) {
      const diff = Math.abs(unique[i].strikePrice! - atmPrice);
      if (diff < minDiff) {
        minDiff = diff;
        atmIdx = i;
      }
    }

    // ATM contract
    selected.push(unique[atmIdx]);

    if (type === 'call') {
      // ITM calls: lower strikes (below ATM)
      for (let i = 1; i <= numItm && atmIdx - i >= 0; i++) {
        selected.push(unique[atmIdx - i]);
      }
      // OTM calls: higher strikes (above ATM)
      for (let i = 1; i <= numOtm && atmIdx + i < unique.length; i++) {
        selected.push(unique[atmIdx + i]);
      }
    } else {
      // ITM puts: higher strikes (above ATM)
      for (let i = 1; i <= numItm && atmIdx + i < unique.length; i++) {
        selected.push(unique[atmIdx + i]);
      }
      // OTM puts: lower strikes (below ATM)
      for (let i = 1; i <= numOtm && atmIdx - i >= 0; i++) {
        selected.push(unique[atmIdx - i]);
      }
    }
  }

  return selected;
}
