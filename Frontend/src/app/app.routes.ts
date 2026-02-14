import { Routes } from "@angular/router";
import { BooksComponent } from "./components/books/books.component";
import { AuthorsComponent } from "./components/authors/authors.component";
import { MarketDataComponent } from "./components/market-data/market-data.component";
import { TickersComponent } from "./components/tickers/tickers.component";
import { TechnicalAnalysisComponent } from "./components/tickers/technical-analysis/technical-analysis.component";

export const routes: Routes = [
  { path: "", redirectTo: "/books", pathMatch: "full" },
  { path: "books", component: BooksComponent },
  { path: "authors", component: AuthorsComponent },
  { path: "market-data", component: MarketDataComponent },
  { path: "tickers", component: TickersComponent },
  { path: "technical-analysis", component: TechnicalAnalysisComponent },
];
