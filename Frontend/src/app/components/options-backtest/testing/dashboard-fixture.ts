import type { RunFigures } from '../options-backtest.types';
import { DAY_ROWS } from './run-fixtures';

type Dashboard = NonNullable<RunFigures['dashboard']>;

/** Authored wire values; UI tests deliberately never derive financial numbers. */
export function dashboard(changes: Partial<Dashboard> = {}): Dashboard {
  return {
    display_annualized_sharpe: '1.23', display_annualized_sortino: '1.99', display_annualized_volatility_percent: '12.3',
    annualization_sessions: '251', daily_observations: 5, risk_free_daily_return: '0',
    annualized_sharpe: '1.23456789', annualized_sortino: '1.987654', annualized_volatility_percent: '12.3',
    average_winning_session_usd: '250.25', average_losing_session_usd: '-62.40', average_session_usd: '154.32',
    return_percent: '12.3', session_profit_factor: '2.45', payoff_ratio: '4.01',
    best_session_usd: '1146.71', worst_session_usd: '-62.40', profit_without_best_session_usd: '87.85', top_five_profit_percent: '110.0',
    recovery_sessions: 3, recovered_session_open_ms: DAY_ROWS[4].session_open_ms,
    longest_losing_streak: 2, longest_underwater_sessions: 3, ongoing_underwater: false,
    daily: DAY_ROWS.map((day, index) => ({ session_open_ms: day.session_open_ms, year: 2026, month: 9, underwater_usd: index === 0 || index === 4 ? '0.00' : '-62.40', rolling_sharpe: index < 2 ? null : '1.25', display_rolling_sharpe: index < 2 ? null : '1.3' })),
    histogram: [{ lower_usd: '-100.00', upper_usd: '0.00', count: 1 }, { lower_usd: '0.00', upper_usd: '1200.00', count: 4 }],
    ...changes,
  };
}
