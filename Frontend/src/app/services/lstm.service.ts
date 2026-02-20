import { HttpClient } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { Observable, interval, map, switchMap, takeWhile, tap } from 'rxjs';
import {
  LstmJobResult,
  LstmJobStatus,
  LstmModelInfo,
  LstmTrainingConfig,
  LstmValidationConfig,
} from '../graphql/lstm-types';

const GRAPHQL_URL = 'http://localhost:5000/graphql';

interface GraphQLResponse {
  data: any;
  errors?: { message: string }[];
}

// --- GraphQL Operations ---

const START_TRAINING = `
  mutation StartLstmTraining(
    $ticker: String!
    $fromDate: String!
    $toDate: String!
    $epochs: Int! = 50
    $sequenceLength: Int! = 60
    $features: String! = "close"
    $mock: Boolean! = false
    $scalerType: String! = "standard"
    $logReturns: Boolean! = false
    $winsorize: Boolean! = false
    $timespan: String! = "day"
    $multiplier: Int! = 1
  ) {
    startLstmTraining(
      ticker: $ticker
      fromDate: $fromDate
      toDate: $toDate
      epochs: $epochs
      sequenceLength: $sequenceLength
      features: $features
      mock: $mock
      scalerType: $scalerType
      logReturns: $logReturns
      winsorize: $winsorize
      timespan: $timespan
      multiplier: $multiplier
    ) {
      success
      jobId
      message
    }
  }
`;

const START_VALIDATION = `
  mutation StartLstmValidation(
    $ticker: String!
    $fromDate: String!
    $toDate: String!
    $folds: Int! = 5
    $epochs: Int! = 20
    $sequenceLength: Int! = 60
    $mock: Boolean! = false
    $scalerType: String! = "standard"
    $logReturns: Boolean! = false
    $winsorize: Boolean! = false
    $timespan: String! = "day"
    $multiplier: Int! = 1
  ) {
    startLstmValidation(
      ticker: $ticker
      fromDate: $fromDate
      toDate: $toDate
      folds: $folds
      epochs: $epochs
      sequenceLength: $sequenceLength
      mock: $mock
      scalerType: $scalerType
      logReturns: $logReturns
      winsorize: $winsorize
      timespan: $timespan
      multiplier: $multiplier
    ) {
      success
      jobId
      message
    }
  }
`;

const GET_JOB_STATUS = `
  query LstmJobStatus($jobId: String!) {
    lstmJobStatus(jobId: $jobId) {
      jobId
      status
      error
      createdAt
      completedAt
      trainResult {
        ticker
        valRmse
        trainRmse
        baselineRmse
        improvement
        epochsCompleted
        bestEpoch
        modelId
        actualValues
        predictedValues
        historyLoss
        historyValLoss
        residuals
        stationarityAdfPvalue
        stationarityKpssPvalue
        stationarityIsStationary
      }
      validateResult {
        ticker
        numFolds
        avgRmse
        avgMae
        avgMape
        avgDirectionalAccuracy
        avgSharpeRatio
        avgMaxDrawdown
        avgProfitFactor
        foldResults {
          fold
          trainSize
          testSize
          rmse
          mae
          mape
          directionalAccuracy
          sharpeRatio
          maxDrawdown
          profitFactor
        }
      }
    }
  }
`;

const GET_MODELS = `
  query LstmModels {
    lstmModels {
      modelId
      ticker
      createdAt
      valRmse
      trainRmse
      baselineRmse
      improvement
      epochsCompleted
      bestEpoch
      sequenceLength
      features
    }
  }
`;

@Injectable({ providedIn: 'root' })
export class LstmService {
  private http = inject(HttpClient);

  startTraining(config: LstmTrainingConfig): Observable<LstmJobResult> {
    return this.http
      .post<GraphQLResponse>(GRAPHQL_URL, {
        query: START_TRAINING,
        variables: config,
      })
      .pipe(
        tap((r) => this.checkErrors(r)),
        map((r) => r.data.startLstmTraining),
      );
  }

  startValidation(config: LstmValidationConfig): Observable<LstmJobResult> {
    return this.http
      .post<GraphQLResponse>(GRAPHQL_URL, {
        query: START_VALIDATION,
        variables: config,
      })
      .pipe(
        tap((r) => this.checkErrors(r)),
        map((r) => r.data.startLstmValidation),
      );
  }

  getJobStatus(jobId: string): Observable<LstmJobStatus> {
    return this.http
      .post<GraphQLResponse>(GRAPHQL_URL, {
        query: GET_JOB_STATUS,
        variables: { jobId },
      })
      .pipe(
        tap((r) => this.checkErrors(r)),
        map((r) => r.data.lstmJobStatus),
      );
  }

  getModels(): Observable<LstmModelInfo[]> {
    return this.http
      .post<GraphQLResponse>(GRAPHQL_URL, {
        query: GET_MODELS,
      })
      .pipe(
        tap((r) => this.checkErrors(r)),
        map((r) => r.data.lstmModels),
      );
  }

  /**
   * Polls job status every `intervalMs` until completed or failed.
   * Emits each intermediate status, completes when terminal.
   */
  pollJob(jobId: string, intervalMs = 3000): Observable<LstmJobStatus> {
    return interval(intervalMs).pipe(
      switchMap(() => this.getJobStatus(jobId)),
      takeWhile(
        (s) => s.status !== 'completed' && s.status !== 'failed',
        true,
      ),
    );
  }

  private checkErrors(response: GraphQLResponse): void {
    if (response.errors?.length) {
      throw new Error(response.errors.map((e) => e.message).join(', '));
    }
  }
}
