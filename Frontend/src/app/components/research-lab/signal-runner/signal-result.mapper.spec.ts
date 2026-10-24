import signalJobResult from '@repo-contracts/fixtures/signal-engine-job-result-v1.json';
import { describe, expect, it } from 'vitest';

import { toSignalEngineResultFromJob, type SignalEngineJobResultRaw } from './signal-result.mapper';

const RAW = signalJobResult as unknown as SignalEngineJobResultRaw;

describe('toSignalEngineResultFromJob', () => {
  it('maps the result the job delivers, with every section set, without failing on any of them', () => {
    const mapped = toSignalEngineResultFromJob(RAW);

    expect(mapped.success).toBe(true);
    expect(mapped.barsUsed).toBe(48_750);
    expect(mapped.graduation?.overallGrade).toBe('B');
    expect(mapped.walkForward?.meanOosSharpe).toBe(0.65);
    expect(mapped.backtestGrid).toHaveLength(1);
  });

  it('reads the regime counts the data-sufficiency section holds as one entry per regime', () => {
    const mapped = toSignalEngineResultFromJob(RAW);

    expect(mapped.dataSufficiency?.regimeCoverage).toEqual([
      { regime: 'low_vol', count: 60 },
      { regime: 'high_vol', count: 25 },
    ]);
    expect(mapped.regimeCoverage.map((entry) => entry.regime).sort()).toEqual(
      Object.keys(RAW.regime_coverage).sort(),
    );
  });
});
