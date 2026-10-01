import type { components } from '../api/broker.types';

export type StrategyValidationSummary = components['schemas']['StrategyValidationEntry'];
export type StrategyValidationDetail = components['schemas']['StrategyValidationDetail'];
export type StrategyValidationCatalog = components['schemas']['StrategyValidationCatalog'];
export type StrategyValidationFlagRequest = components['schemas']['StrategyValidationFlagRequest'];
export type StrategyValidationRefreshResult = components['schemas']['StrategyValidationRefreshResult'];
export type StrategyValidationFlagEvent = components['schemas']['StrategyValidationFlagEvent'];
export type StrategyReferenceCode = components['schemas']['StrategyReferenceCode'];
export type StrategyProofDossier = components['schemas']['StrategyProofDossier'];
export type StrategyProofStage = components['schemas']['StrategyProofStage'];

export type StrategyValidationFlag = StrategyValidationFlagRequest['flag'];
