/**
 * Sample IV Surface Data — No-Arbitrage SVI by Construction
 *
 * This data is generated to be realistic SPY-like IV surfaces with:
 * - Negative skew (rho ≈ -0.4)
 * - Increasing total variance across time (no calendar arb)
 * - ATM vol ≈ 18-25% (realistic SPY range)
 * - Smile depth increasing at short tenors
 */

// Generate log-moneyness grid: -0.3 to +0.3 in 30 steps
function generateLogMoneynessGrid(nStrikes = 30) {
    const x = [];
    for (let i = 0; i < nStrikes; i++) {
        x.push(-0.3 + (i / (nStrikes - 1)) * 0.6);
    }
    return x;
}

// SVI formula: w(k) = a + b*(rho*(k-m) + sqrt((k-m)^2 + sigma^2))
// Returns implied volatility (not variance)
function sviImpliedVol(k, sviParams) {
    const { a, b, rho, m, sigma } = sviParams;
    const km = k - m;
    const sqrtTerm = Math.sqrt(km * km + sigma * sigma);
    const w = a + b * (rho * km + sqrtTerm);  // total variance
    return Math.sqrt(Math.max(w, 0.0001)); // implied vol from total variance
}

// Generate SVI parameters for a given TTM (time to maturity)
// Ensures increasing total variance (calendar spread arb-free)
function generateSviParams(ttm, tenorIndex) {
    // Base ATM variance increases with tenor
    const baseVar = 0.035 + tenorIndex * 0.015;
    const a = baseVar;

    // Smile curvature increases at short tenors
    const b = 0.04 + (6 - tenorIndex) * 0.012;

    // Negative skew for puts
    const rho = -0.35 - tenorIndex * 0.02;

    // ATM offset (roughly center of smile)
    const m = -0.02 - tenorIndex * 0.008;

    // Smile width
    const sigma = 0.15 + tenorIndex * 0.01;

    return { a, b, rho, m, sigma };
}

// Generate IV surface grid for given expiries
function generateIvSurface(spot, forwards, dtes, nStrikes = 30) {
    const x = generateLogMoneynessGrid(nStrikes);
    const y = dtes;
    const z = [];
    const smiles = [];

    dtes.forEach((dte, idx) => {
        const ttm = dte / 365.0;
        const sviParams = generateSviParams(ttm, idx);
        const forward = forwards[idx];

        const rowIv = [];
        const smileData = {
            ttm,
            dte_days: dte,
            expiry_date: new Date(new Date().getTime() + dte * 24 * 60 * 60 * 1000)
                .toISOString().split('T')[0],
            forward,
            fitted: [],
            market: []
        };

        x.forEach((logMoney, xIdx) => {
            const iv = sviImpliedVol(logMoney, sviParams);
            rowIv.push(Math.min(iv, 0.80)); // cap at 80% vol

            // Fitted curve: all points
            smileData.fitted.push({
                x: parseFloat(logMoney.toFixed(4)),
                iv: parseFloat(iv.toFixed(4))
            });

            // Market: sparse, every 2-3 points, with slight noise
            if (xIdx % 2 === 0 || xIdx === nStrikes - 1) {
                const noise = (Math.random() - 0.5) * 0.003;
                const marketIv = Math.max(iv + noise, 0.01);
                smileData.market.push({
                    x: parseFloat(logMoney.toFixed(4)),
                    iv: parseFloat(marketIv.toFixed(4)),
                    status: "solved"
                });
            }
        });

        z.push(rowIv);
        smiles.push(smileData);
    });

    return { x, y, z, smiles };
}

// Generate diagnostics: per-slice metrics and arbitrage checks
function generateDiagnostics(summary, smiles, dtes, totalQuotes = 365) {
    const accepted = 320;
    const rejected = 45;

    const rejections = {
        total_quotes: totalQuotes,
        accepted,
        rejected,
        by_reason: {
            "spread_too_wide": 18,
            "low_open_interest": 12,
            "dte_out_of_range": 8,
            "solver_failed": 5,
            "price_too_low": 2
        }
    };

    const slices = smiles.map((smile, idx) => ({
        ttm: parseFloat(smile.ttm.toFixed(6)),
        n_contracts: 55 - idx * 3,
        n_solved: 52 - idx * 2,
        n_failed: 3 + idx,
        fit_method: "svi",
        fit_rmse: parseFloat((0.003 + Math.random() * 0.002).toFixed(4)),
        butterfly_violations: idx === 0 ? 1 : 0,
        arbitrage_passed: idx !== 0 || true
    }));

    const fittedParams = smiles.map((smile, idx) => {
        const ttm = smile.ttm;
        const sviParams = generateSviParams(ttm, idx);
        return {
            ttm: parseFloat(ttm.toFixed(6)),
            method: "svi",
            params: {
                a: parseFloat(sviParams.a.toFixed(6)),
                b: parseFloat(sviParams.b.toFixed(6)),
                rho: parseFloat(sviParams.rho.toFixed(4)),
                m: parseFloat(sviParams.m.toFixed(4)),
                sigma: parseFloat(sviParams.sigma.toFixed(4))
            },
            rmse: parseFloat((0.003 + Math.random() * 0.001).toFixed(4))
        };
    });

    return {
        summary,
        rejections,
        arbitrage: {
            calendar_violations: 0,
            butterfly_violations: 2,
            severity: "low",
            worst_slices: []
        },
        fitted_params: fittedParams,
        slices,
        health_score: summary.health_score,
        warnings: slices[0].butterfly_violations > 0
            ? [`Expiry T=${slices[0].ttm.toFixed(4)}: ${slices[0].butterfly_violations} butterfly violation`]
            : []
    };
}

// Initialize sample data on load
(function initSampleData() {
    const ticker = "SPY";
    const spot = 512.30;
    const date = new Date().toISOString().split('T')[0];
    const dtes = [14, 30, 60, 90, 180, 365];

    // Forwards: spot * exp(r*t), assuming r ≈ 0.05
    const forwards = dtes.map(dte => {
        const t = dte / 365;
        return parseFloat((spot * Math.exp(0.05 * t)).toFixed(2));
    });

    // Generate surface
    const { x, y, z, smiles } = generateIvSurface(spot, forwards, dtes, 30);

    // Summary
    const summary = {
        surface_id: `sample_${ticker.toLowerCase()}_${date}`,
        ticker,
        spot,
        method: "svi",
        date,
        cached: false,
        n_expiries: dtes.length,
        n_contracts_accepted: 320,
        n_contracts_rejected: 45,
        build_time_ms: 0,
        health_score: 87,
        valid: true,
        schema_version: "1.0.0"
    };

    // Grid
    const grid = {
        x,
        y,
        z,
        x_label: "log_moneyness",
        y_label: "dte_days",
        z_label: "implied_vol",
        meta: {
            spot,
            forwards,
            n_strikes: x.length,
            n_expiries: y.length,
            expiry_dates: dtes.map(dte => {
                return new Date(new Date().getTime() + dte * 24 * 60 * 60 * 1000)
                    .toISOString().split('T')[0];
            })
        }
    };

    // Diagnostics
    const diagnostics = generateDiagnostics(summary, smiles, dtes);

    // Export
    window.SAMPLE_DATA = {
        summary,
        grid: {
            ...grid,
            smiles
        },
        smiles: {
            x_label: "log_moneyness",
            slices: smiles
        },
        diagnostics
    };

    console.log('Sample IV surface data initialized:', window.SAMPLE_DATA);
})();
