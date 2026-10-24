import { RV_WINDOWS, type RealizedVsIvData } from '../../components/edge/services/edge-data.model';
import {
  projectRealizedVsIvResult,
  type RealizedVsIvResultRaw,
} from '../../components/edge/services/realized-vs-iv-projection';
import {
  toBatchResearchResult,
  type CrossSectionalJobResultRaw,
} from '../../components/research-lab/batch-runner/cross-sectional-result.mapper';
import {
  toResearchResult,
  type FeatureResearchJobResultRaw,
} from '../../components/research-lab/feature-runner/feature-result.mapper';
import type { IndicatorReliabilityResponse } from '../../components/research-lab/indicator-reliability/indicator-reliability.models';
import {
  toSignalEngineResultFromJob,
  type SignalEngineJobResultRaw,
} from '../../components/research-lab/signal-runner/signal-result.mapper';
import type { BatchResearchResult, ResearchResult, SignalEngineResult } from '../../services/research.service';
import { StoredInputs } from './stored-inputs';
import type { StoredStudyRecord } from './stored-study';

/**
 * A saved run's result, in the model its study's own result view draws.
 *
 * The result was stored as the study delivered it, so each study maps it with the mapper its runner page uses for a
 * live one; nothing here recomputes or reinterprets a figure. A run that left no result (failed, cancelled) has
 * nothing to draw, and a result the browser cannot map is said to be unreadable rather than drawn from a guess.
 */
export type StoredStudyView =
  | { readonly kind: 'feature'; readonly result: ResearchResult }
  | { readonly kind: 'signal'; readonly result: SignalEngineResult }
  | { readonly kind: 'cross-sectional'; readonly result: BatchResearchResult }
  | { readonly kind: 'indicator-reliability'; readonly result: IndicatorReliabilityResponse }
  | { readonly kind: 'realized-vs-iv'; readonly data: RealizedVsIvData }
  | { readonly kind: 'none' }
  | { readonly kind: 'unreadable'; readonly reason: string };

export function storedStudyView(record: StoredStudyRecord): StoredStudyView {
  const result: unknown = record.result;
  if (result === null || typeof result !== 'object') return { kind: 'none' };
  if ((result as { success?: unknown }).success === false) return { kind: 'none' };
  try {
    switch (record.study) {
      case 'feature-study':
        return { kind: 'feature', result: toResearchResult(result as FeatureResearchJobResultRaw) };
      case 'signal-study':
        return { kind: 'signal', result: toSignalEngineResultFromJob(result as SignalEngineJobResultRaw) };
      case 'cross-sectional-study':
        return { kind: 'cross-sectional', result: toBatchResearchResult(result as CrossSectionalJobResultRaw) };
      case 'indicator-reliability':
        return { kind: 'indicator-reliability', result: result as IndicatorReliabilityResponse };
      case 'realized-vs-iv':
        return realizedVsIvView(record, result as RealizedVsIvResultRaw);
    }
  } catch (error) {
    // A stored result the mapper cannot read (a field the study no longer sends): the record stays, the drawing does not.
    const detail = error instanceof Error ? error.message : 'its shape is not one this page knows';
    return { kind: 'unreadable', reason: detail };
  }
}

function realizedVsIvView(record: StoredStudyRecord, stored: RealizedVsIvResultRaw): StoredStudyView {
  const inputs = new StoredInputs(record.inputs);
  const windows = inputs.has('windows') ? inputs.numbers('windows') : RV_WINDOWS;
  const projected = projectRealizedVsIvResult(stored, windows);
  // A saved run never carries the live IV marker: it is a reading of the moment it was taken.
  return {
    kind: 'realized-vs-iv',
    data: {
      ...projected,
      liveIv30: { kind: 'unavailable', reason: 'A saved run does not keep the current-IV marker: it is a reading of the moment it was taken.' },
    },
  };
}
