/** Browser symbol-shape parity with PythonDataService/app/broker/contract/models.py::BrokerOrderLeg.symbol. */
import { describe, expect, it } from 'vitest';

import cases from './us-equity-symbol.parity.json';
import { isUsEquitySymbol } from './us-equity-symbol';

describe('US equity symbol parity', () => {
  it('pins the Python canonical', () => {
    expect(cases.schema_version).toBe(1);
    expect(cases.canonical).toBe('PythonDataService/app/broker/contract/models.py::BrokerOrderLeg.symbol');
  });

  it.each(cases.cases)('$symbol', ({ symbol, accepted }) => {
    expect(isUsEquitySymbol(symbol)).toBe(accepted);
  });
});
