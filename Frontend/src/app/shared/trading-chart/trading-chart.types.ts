export interface TradingCandle {
  timeMs: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
}

export interface TradingPoint {
  timeMs: number;
  value: number;
}

export interface TradingSeries {
  id: string;
  name: string;
  color: string;
  type: "line" | "area" | "histogram";
  points: TradingPoint[];
  lineWidth?: 1 | 2 | 3 | 4;
  /** Display interpolation only; producer values remain unchanged. */
  lineType?: "straight" | "steps";
}

export interface TradingSubPane {
  id: string;
  label: string;
  series: TradingSeries[];
  referenceLevels?: number[];
}

export interface TradingMarker {
  timeMs: number;
  position: "aboveBar" | "belowBar";
  color: string;
  shape: "arrowUp" | "arrowDown" | "circle" | "square";
  text: string;
}

export interface TradingIndicatorChip {
  id: string;
  label: string;
  color: string;
}
