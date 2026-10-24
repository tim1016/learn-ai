import { render, screen } from "@testing-library/angular";
import { describe, expect, it } from "vitest";
import type { RealizedVsIvData } from "../services/edge-data.model";
import { RealizedVsIvReportComponent } from "./realized-vs-iv-report.component";

/** A stored result: real-shaped, and without the live IV30 marker a fresh Compute attaches. */
function storedData(): RealizedVsIvData {
  const N = 12;
  const flat = (v: number) => Array.from({ length: N }, () => v);
  return {
    provenance: "real",
    symbol: "SPY",
    N,
    dates: Array.from({ length: N }, (_, i) => new Date(Date.UTC(2026, 0, 5 + i, 14))),
    candles: Array.from({ length: N }, (_, i) => ({ o: 500 + i, h: 503 + i, l: 498 + i, c: 501 + i, v: 1e6 })),
    iv30: flat(0.21),
    iv30Trd252: flat(0.2),
    rvYZ: flat(0.14),
    rvYzByWindow: [5, 10, 30].map((window) => ({ window, values: flat(0.14) })),
    rvHf21d: flat(0.15),
    vrpForward: flat(0.05),
    vrpZ: flat(0),
    vrpHistogram: [],
    signals: [],
    coverage: { forwardBlindTail: 2, ivFirstTsMs: Date.UTC(2026, 0, 6, 14) },
    ivConfidence: null,
    liveIv30: null,
  };
}

describe("RealizedVsIvReportComponent", () => {
  it("renders the real-data tag and the readout from the data alone, with no live IV30 row", async () => {
    await render(RealizedVsIvReportComponent, { inputs: { data: storedData() } });

    expect(screen.getByTestId("rviv-source-tag").textContent).toContain("SPY");
    const readout = screen.getByTestId("rviv-readout").textContent ?? "";
    expect.soft(readout, "IV30 from the data").toContain("21.0%");
    expect.soft(readout, "30-bar Yang-Zhang RV from the data").toContain("14.0%");
    expect(screen.queryByTestId("live-iv30-readout"), "a stored result has no live marker").toBeNull();
    expect(document.querySelectorAll("canvas"), "price chart and VRP histogram").toHaveLength(2);
  });
});
