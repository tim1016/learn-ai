import type { components } from '../api/broker.types';

export type StrategyValidationSummary = components['schemas']['StrategyValidationEntry'];
export type StrategyValidationDetail = components['schemas']['StrategyValidationDetail'];
export type StrategyValidationCatalog = components['schemas']['StrategyValidationCatalog'];
export type StrategyValidationFlagRequest = components['schemas']['StrategyValidationFlagRequest'];
export type StrategyValidationRefreshResult = components['schemas']['StrategyValidationRefreshResult'];
export type StrategyValidationDiagnostics = components['schemas']['StrategyValidationDiagnostics'];
export type StrategyEvidenceSnapshot = components['schemas']['StrategyEvidenceSnapshot'];
export type StrategyBehavioralEquivalence = components['schemas']['StrategyBehavioralEquivalence'];
export type StrategyValidationFlagEvent = components['schemas']['StrategyValidationFlagEvent'];
export type StrategyReferenceCode = components['schemas']['StrategyReferenceCode'];
export type StrategyArtifactCheck = components['schemas']['StrategyArtifactCheck'];
export type StrategyProofAction = components['schemas']['StrategyProofAction'];
export type StrategyProofDossier = components['schemas']['StrategyProofDossier'];
export type StrategyProofStage = components['schemas']['StrategyProofStage'];

export type StrategyValidationState = StrategyValidationSummary['validation_state'];
export type StrategyValidationFlag = StrategyValidationFlagRequest['flag'];
export type BehavioralEquivalenceVerdict = StrategyBehavioralEquivalence['verdict'];
export type StrategyCategory = StrategyValidationSummary['strategy_category'];
export type StrategyProofState = StrategyProofDossier['state'];
export type StrategyProofStageState = StrategyProofStage['state'];
export type StrategyArtifactState = StrategyArtifactCheck['state'];
