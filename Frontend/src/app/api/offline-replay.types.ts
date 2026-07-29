export type OfflineReplaySymbol = 'SPY' | 'TSLA';
export type OfflineReplaySpeed = 1 | 10 | 60;
export type OfflineReplayStatus =
  | 'preparing'
  | 'warming_up'
  | 'running'
  | 'paused'
  | 'stopping'
  | 'completed'
  | 'stopped'
  | 'failed'
  | 'interrupted';
export type OfflineReplayBotStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'stopped';
export type OfflineReplayCommandAction =
  | 'pause'
  | 'resume'
  | 'step'
  | 'set_speed'
  | 'stop';

export interface OfflineReplayCatalogSession {
  session_date_ms: number;
  session_open_ms: number;
  session_close_ms: number;
  eligible: boolean;
  cached_symbols: OfflineReplaySymbol[];
}

export interface OfflineReplayCatalogResponse {
  sessions: OfflineReplayCatalogSession[];
  recommended_session_date_ms: number | null;
}

export interface OfflineReplayCreateRequest {
  session_date_ms: number;
  symbols: OfflineReplaySymbol[];
  playback_minutes: 30 | 60;
  speed: OfflineReplaySpeed;
  initial_cash_usd: string;
  auto_fetch: boolean;
}

export interface OfflineReplayCommandRequest {
  action: OfflineReplayCommandAction;
  speed?: OfflineReplaySpeed;
}

export interface OfflineReplayBotResult {
  symbol: OfflineReplaySymbol;
  status: OfflineReplayBotStatus;
  run_id: string;
  bars_processed: number;
  decisions_emitted: number;
  orders_submitted: number;
  fills_observed: number;
  open_position_quantity: number;
  final_equity_usd: string;
  data_sha256: string;
  failure_code: string | null;
  failure_message: string | null;
}

export interface OfflineReplaySession {
  session_id: string;
  status: OfflineReplayStatus;
  strategy_key: 'ema_crossover_signal';
  session_date_ms: number;
  session_open_ms: number;
  session_close_ms: number;
  warmup_end_ms: number;
  playback_end_ms: number;
  playhead_ms: number | null;
  playback_minutes: 30 | 60;
  speed: OfflineReplaySpeed;
  created_at_ms: number;
  started_at_ms: number | null;
  completed_at_ms: number | null;
  symbols: OfflineReplaySymbol[];
  bots: OfflineReplayBotResult[];
  failure_code: string | null;
  failure_message: string | null;
}

export interface OfflineReplaySessionListResponse {
  sessions: OfflineReplaySession[];
}
