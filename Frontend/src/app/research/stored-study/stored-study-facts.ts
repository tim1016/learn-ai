import { StoredInputs } from './stored-inputs';
import type { StoredStudyRecord } from './stored-study';

/** One thing the run was asked, in the words its study's page uses for the input. */
export interface StoredFact {
  readonly label: string;
  readonly value: string;
}

/** `"minute"` and `1` read "1 minute"; the unit is the request's own. */
function bars(inputs: StoredInputs): string {
  return `${inputs.number('multiplier')} ${inputs.text('timespan')}`;
}

function onOff(value: boolean): string {
  return value ? 'on' : 'off';
}

/**
 * What a saved run was asked, read from its stored inputs: the same facts the study's page lists beside a result
 * (its instrument, its indicator, its options), so the saved run says what the live one would. The window is drawn
 * from the record's own window, as dates in ET, and is not repeated here. A field the record cannot give is left
 * out, never replaced by a default nobody entered.
 */
export function storedFacts(record: StoredStudyRecord): readonly StoredFact[] {
  const inputs = new StoredInputs(record.inputs);
  switch (record.study) {
    case 'feature-study':
      return [
        { label: 'Instrument', value: inputs.text('symbol').toUpperCase() },
        { label: 'Bars', value: bars(inputs) },
        { label: 'Indicator', value: inputs.text('feature_name') },
      ];
    case 'signal-study':
      return [
        { label: 'Instrument', value: inputs.text('symbol').toUpperCase() },
        { label: 'Bars', value: bars(inputs) },
        { label: 'Indicator', value: inputs.text('feature_name') },
        { label: 'Flip sign', value: onOff(inputs.boolean('flip_sign')) },
        { label: 'Regime gate', value: onOff(inputs.boolean('regime_gate_enabled')) },
      ];
    case 'cross-sectional-study':
      return [
        { label: 'Universe', value: inputs.texts('symbols').map((symbol) => symbol.toUpperCase()).join(', ') },
        { label: 'Feature', value: inputs.text('feature_name') },
        { label: 'Target', value: inputs.text('target_type') },
      ];
    case 'indicator-reliability': {
      const params = Object.entries(inputs.numberMap('indicator_params'));
      const named = params.length === 0 ? '' : ` (${params.map(([key, value]) => `${key} ${value}`).join(', ')})`;
      return [
        { label: 'Instrument', value: inputs.text('symbol').toUpperCase() },
        { label: 'Indicator', value: `${inputs.text('indicator_name')}${named}` },
        { label: 'Horizons', value: `${[...inputs.numbers('horizons')].sort((a, b) => a - b).join(', ')} bars` },
        { label: 'Slope IC', value: inputs.boolean('include_slope') ? 'included' : 'not included' },
      ];
    }
    case 'realized-vs-iv':
      return [
        { label: 'Instrument', value: inputs.text('symbol').toUpperCase() },
        { label: 'Bars sent as', value: inputs.text('bar_size') },
        { label: 'Bars', value: `${inputs.record('bars').number('count')}` },
        { label: 'Tenor', value: `${inputs.number('tenor_days')} days` },
        { label: 'HF session', value: inputs.text('session') },
      ];
  }
}
