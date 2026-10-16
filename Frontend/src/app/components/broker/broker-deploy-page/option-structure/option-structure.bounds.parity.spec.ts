/** Form feedback parity with PythonDataService/app/options/bounds.py::Bounds.fault (ADR 0068). */
import { describe, expect, it } from 'vitest';

import cases from './option-structure.bounds.parity.json';
import { termProblem, type OptionValueBounds } from './option-structure';

const fixture = cases as {
  schema_version: number;
  canonical: string;
  cases: {
    name: string;
    value: string;
    bounds: Pick<OptionValueBounds, 'min' | 'min_included' | 'max' | 'max_included' | 'step'>;
    accepted: boolean;
  }[];
};

describe('option order bounds parity', () => {
  it('pins the Python canonical and fixture version', () => {
    expect(fixture.schema_version).toBe(1);
    expect(fixture.canonical).toBe('PythonDataService/app/options/bounds.py::Bounds.fault');
  });

  it.each(fixture.cases)('$name', ({ value, bounds, accepted }) => {
    const offered: OptionValueBounds = { ...bounds, min_ms: null, max_ms: null, sentence: 'a valid number' };
    expect(termProblem(value, offered) === null).toBe(accepted);
  });
});
