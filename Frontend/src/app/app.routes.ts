import { Routes } from "@angular/router";
import { MarketDataComponent } from "./components/market-data/market-data.component";
import { TickersComponent } from "./components/tickers/tickers.component";
import { TechnicalAnalysisComponent } from "./components/tickers/technical-analysis/technical-analysis.component";
import { StockAnalysisComponent } from "./components/stock-analysis/stock-analysis.component";
import { ChunkDetailComponent } from "./components/stock-analysis/chunk-detail/chunk-detail.component";
import { DayDetailComponent } from "./components/stock-analysis/day-detail/day-detail.component";
import { TickerExplorerComponent } from "./components/ticker-explorer/ticker-explorer.component";
import { StrategyLabComponent } from "./components/strategy-lab/strategy-lab.component";
import { OptionsHistoryComponent } from "./components/options-history/options-history.component";
import { SnapshotsComponent } from "./components/snapshots/snapshots.component";
import { TrackedInstrumentsComponent } from "./components/tracked-instruments/tracked-instruments.component";

export const routes: Routes = [
  { path: "", redirectTo: "/market-data", pathMatch: "full" },
  { path: "market-data", component: MarketDataComponent },
  { path: "tickers", component: TickersComponent },
  { path: "technical-analysis", component: TechnicalAnalysisComponent },
  { path: "stock-analysis", component: StockAnalysisComponent },
  { path: "stock-analysis/chunk/:ticker/:fromDate/:toDate", component: ChunkDetailComponent },
  { path: "stock-analysis/day/:ticker/:date", component: DayDetailComponent },
  { path: "options-chain", component: TickerExplorerComponent },
  { path: "strategy-lab", component: StrategyLabComponent },
  { path: "options-history", component: OptionsHistoryComponent },
  { path: "snapshots", component: SnapshotsComponent },
  { path: "tracked-instruments", component: TrackedInstrumentsComponent },
];
