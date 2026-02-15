export type ChunkStatus = 'pending' | 'cached' | 'fetching' | 'complete' | 'error';

export interface FetchChunk {
  index: number;
  fromDate: string;
  toDate: string;
  status: ChunkStatus;
  barCount: number;
  durationMs: number;
  errorMessage?: string;
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
