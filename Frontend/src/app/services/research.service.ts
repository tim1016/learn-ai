import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, map, tap } from 'rxjs';
import { environment } from '../../environments/environment';

const GRAPHQL_URL = environment.backendUrl;

// ─── Interfaces ────────────────────────────────────────────

export interface QuantileBin {
  binNumber: number;
  lowerBound: number;
  upperBound: number;
  meanReturn: number;
  count: number;
}

export interface ResearchResult {
  success: boolean;
  ticker: string;
  featureName: string;
  startDate: string;
  endDate: string;
  barsUsed: number;
  meanIC: number;
  icTStat: number;
  icPValue: number;
  icValues: number[];
  icDates: string[];
  adfPvalue: number;
  kpssPvalue: number;
  isStationary: boolean;
  quantileBins: QuantileBin[];
  isMonotonic: boolean;
  monotonicityRatio: number;
  passedValidation: boolean;
  error?: string;
}

export interface ResearchExperiment {
  id: number;
  ticker: string;
  featureName: string;
  startDate: string;
  endDate: string;
  barsUsed: number;
  meanIC: number;
  icTStat: number;
  icPValue: number;
  adfPValue: number;
  kpssPValue: number;
  isStationary: boolean;
  passedValidation: boolean;
  monotonicityRatio: number;
  isMonotonic: boolean;
  createdAt: string;
}

export interface FeatureInfo {
  name: string;
  display_name: string;
  formula_latex: string;
  variables: string;
  example: string;
  interpretation: string;
  implementation_note: string;
  window: number;
  category: string;
}

export interface RunFeatureResearchInput {
  ticker: string;
  featureName: string;
  fromDate: string;
  toDate: string;
  timespan?: string;
  multiplier?: number;
}

// ─── GraphQL Queries ───────────────────────────────────────

const RUN_FEATURE_RESEARCH_MUTATION = `
  mutation RunFeatureResearch(
    $ticker: String!
    $featureName: String!
    $fromDate: String!
    $toDate: String!
    $timespan: String! = "minute"
    $multiplier: Int! = 1
  ) {
    runFeatureResearch(
      ticker: $ticker
      featureName: $featureName
      fromDate: $fromDate
      toDate: $toDate
      timespan: $timespan
      multiplier: $multiplier
    ) {
      success ticker featureName startDate endDate barsUsed
      meanIC icTStat icPValue icValues icDates
      adfPvalue kpssPvalue isStationary
      quantileBins { binNumber lowerBound upperBound meanReturn count }
      isMonotonic monotonicityRatio
      passedValidation error
    }
  }
`;

const GET_RESEARCH_EXPERIMENTS_QUERY = `
  query GetResearchExperiments($ticker: String!) {
    getResearchExperiments(ticker: $ticker) {
      id ticker featureName startDate endDate barsUsed
      meanIC icTStat icPValue adfPValue kpssPValue
      isStationary passedValidation
      monotonicityRatio isMonotonic createdAt
    }
  }
`;

const GET_RESEARCH_EXPERIMENT_QUERY = `
  query GetResearchExperiment($id: Int!) {
    getResearchExperiment(id: $id) {
      id ticker featureName startDate endDate barsUsed
      meanIC icTStat icPValue adfPValue kpssPValue
      isStationary passedValidation
      monotonicityRatio isMonotonic createdAt
    }
  }
`;

// ─── Response Types ────────────────────────────────────────

interface RunResearchResponse {
  data: { runFeatureResearch: ResearchResult };
  errors?: { message: string }[];
}

interface GetExperimentsResponse {
  data: { getResearchExperiments: ResearchExperiment[] };
  errors?: { message: string }[];
}

interface GetExperimentResponse {
  data: { getResearchExperiment: ResearchExperiment | null };
  errors?: { message: string }[];
}

// ─── Service ───────────────────────────────────────────────

@Injectable({
  providedIn: 'root'
})
export class ResearchService {
  private http = inject(HttpClient);

  runFeatureResearch(input: RunFeatureResearchInput): Observable<ResearchResult> {
    return this.http
      .post<RunResearchResponse>(GRAPHQL_URL, {
        query: RUN_FEATURE_RESEARCH_MUTATION,
        variables: {
          ticker: input.ticker,
          featureName: input.featureName,
          fromDate: input.fromDate,
          toDate: input.toDate,
          timespan: input.timespan ?? 'minute',
          multiplier: input.multiplier ?? 1,
        }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.runFeatureResearch)
      );
  }

  getExperiments(ticker: string): Observable<ResearchExperiment[]> {
    return this.http
      .post<GetExperimentsResponse>(GRAPHQL_URL, {
        query: GET_RESEARCH_EXPERIMENTS_QUERY,
        variables: { ticker }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.getResearchExperiments)
      );
  }

  getExperiment(id: number): Observable<ResearchExperiment | null> {
    return this.http
      .post<GetExperimentResponse>(GRAPHQL_URL, {
        query: GET_RESEARCH_EXPERIMENT_QUERY,
        variables: { id }
      })
      .pipe(
        tap(response => {
          if (response.errors?.length) {
            throw new Error(response.errors.map(e => e.message).join(', '));
          }
        }),
        map(response => response.data.getResearchExperiment)
      );
  }
}
