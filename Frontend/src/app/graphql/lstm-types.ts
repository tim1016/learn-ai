export interface LstmTrainingConfig {
  ticker: string;
  fromDate: string;
  toDate: string;
  epochs: number;
  sequenceLength: number;
  features: string;
  mock: boolean;
}

export interface LstmValidationConfig {
  ticker: string;
  fromDate: string;
  toDate: string;
  folds: number;
  epochs: number;
  sequenceLength: number;
  mock: boolean;
}

export interface LstmJobResult {
  success: boolean;
  jobId: string;
  message: string | null;
}

export interface LstmJobStatus {
  jobId: string;
  status: string;
  trainResult: LstmTrainResult | null;
  validateResult: LstmValidateResult | null;
  error: string | null;
  createdAt: string | null;
  completedAt: string | null;
}

export interface LstmTrainResult {
  ticker: string;
  valRmse: number;
  trainRmse: number;
  baselineRmse: number;
  improvement: number;
  epochsCompleted: number;
  bestEpoch: number;
  modelId: string;
  actualValues: number[];
  predictedValues: number[];
  historyLoss: number[];
  historyValLoss: number[];
  residuals: number[];
}

export interface LstmValidateResult {
  ticker: string;
  numFolds: number;
  avgRmse: number;
  avgMae: number;
  avgMape: number;
  avgDirectionalAccuracy: number;
  foldResults: LstmFoldResult[];
}

export interface LstmFoldResult {
  fold: number;
  trainSize: number;
  testSize: number;
  rmse: number;
  mae: number;
  mape: number;
  directionalAccuracy: number;
}

export interface LstmModelInfo {
  modelId: string;
  ticker: string;
  createdAt: string;
  valRmse: number;
  trainRmse: number;
  baselineRmse: number;
  improvement: number;
  epochsCompleted: number;
  bestEpoch: number;
  sequenceLength: number;
  features: string[];
}
