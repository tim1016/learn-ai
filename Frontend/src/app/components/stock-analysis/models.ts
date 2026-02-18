export type ChunkStatus = 'pending' | 'cached' | 'fetching' | 'complete' | 'error';
export type AtmMethod = 'previousClose' | 'currentOpen';

export interface SelectedContract {
  ticker: string;
  contractType: string;
  strikePrice: number;
  expirationDate: string;
}

export interface TradingDay {
  date: string;
  stockBarCount: number;
  optionsStatus: ChunkStatus;
  optionsFetchedCount: number;
  optionsContractCount: number;
  contracts: SelectedContract[];
}

export interface FetchChunk {
  index: number;
  fromDate: string;
  toDate: string;
  status: ChunkStatus;
  barCount: number;
  durationMs: number;
  errorMessage?: string;
  optionsStatus?: ChunkStatus;
  optionsContractCount?: number;
  optionsFetchedCount?: number;
  tradingDays?: TradingDay[];
}

export interface ProgressStats {
  totalChunks: number;
  completedChunks: number;
  cachedChunks: number;
  errorChunks: number;
  totalBars: number;
  earliestDate: string | null;
  latestDate: string | null;
}
