/**
 * TypeScript interfaces for the Python Market Monitor REST API responses.
 * These use snake_case property names matching the Python FastAPI JSON output
 * (frontend calls the Python service directly, not through the .NET GraphQL layer).
 */

export interface MarketHolidayEvent {
  date: string | null;
  name: string | null;
  status: string | null;       // "Closed" or "Early Close"
  open: string | null;         // ISO time string for early close open
  close: string | null;        // ISO time string for early close close
  exchanges: string[];
}

export interface MarketHolidaysResponse {
  success: boolean;
  events: MarketHolidayEvent[];
  count: number;
  error: string | null;
}

export interface ExchangeStatus {
  nyse: string | null;
  nasdaq: string | null;
  otc: string | null;
}

export interface MarketStatusResponse {
  success: boolean;
  market: string;              // "open" | "closed" | "extended-hours"
  exchanges: ExchangeStatus;
  early_hours: boolean;
  after_hours: boolean;
  server_time: string;
  server_time_readable: string;
  error: string | null;
}

export interface MarketDashboardResponse {
  success: boolean;
  status: MarketStatusResponse | null;
  holidays: MarketHolidaysResponse | null;
  error: string | null;
}
