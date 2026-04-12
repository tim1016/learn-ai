/**
 * IV Dashboard — Plotly Panel Renderers
 *
 * 6 panels: Surface, Smiles, Market vs Fit, Term Structure, Diagnostics, Rejections
 */

// ============================================================================
// Shared Plotly Configuration
// ============================================================================

const PLOTLY_LAYOUT_DEFAULTS = {
    paper_bgcolor: '#16213e',
    plot_bgcolor: '#16213e',
    font: { color: '#e0e0e0', family: 'system-ui, sans-serif' },
    margin: { l: 50, r: 20, t: 30, b: 40 },
    hovermode: 'closest',
    showlegend: true,
    legend: {
        bgcolor: 'rgba(0, 0, 0, 0.3)',
        bordercolor: '#2d3561',
        borderwidth: 1,
        font: { size: 11 }
    },
    xaxis: {
        gridcolor: '#2d356166',
        zeroline: false,
        showgrid: true
    },
    yaxis: {
        gridcolor: '#2d356166',
        zeroline: false,
        showgrid: true
    }
};

const PLOTLY_CONFIG = {
    responsive: true,
    displayModeBar: false,
    staticPlot: false
};

// ============================================================================
// Panel 1: 3D Surface
// ============================================================================

function renderSurface(containerId, gridData, axis) {
    const container = document.getElementById(containerId);
    if (!container) return;

    if (!gridData || !gridData.x || !gridData.z) {
        container.innerHTML = '<div class="empty-state">No grid data available</div>';
        return;
    }

    const { x, y, z, x_label } = gridData;

    const trace = {
        x, y, z,
        type: 'surface',
        colorscale: 'Viridis',
        showscale: true,
        colorbar: {
            title: 'IV',
            tickfont: { color: '#e0e0e0' },
            tickcolor: '#e0e0e0',
            thickness: 15,
            len: 0.6
        },
        contours: {
            z: {
                show: true,
                usecolorscale: true,
                highlightcolor: 'limegreen',
                project: { z: true }
            }
        }
    };

    const layout = {
        ...PLOTLY_LAYOUT_DEFAULTS,
        title: {
            text: 'IV Surface',
            font: { size: 14, color: '#e0e0e0' }
        },
        scene: {
            xaxis: {
                title: x_label === 'log_moneyness' ? 'Log-Moneyness' : axis,
                gridcolor: '#2d356166',
                showbackground: true,
                backgroundcolor: '#0f3460'
            },
            yaxis: {
                title: 'DTE (days)',
                gridcolor: '#2d356166',
                showbackground: true,
                backgroundcolor: '#0f3460'
            },
            zaxis: {
                title: 'IV',
                gridcolor: '#2d356166',
                showbackground: true,
                backgroundcolor: '#0f3460'
            },
            camera: {
                eye: { x: 1.5, y: 1.5, z: 1.3 }
            }
        },
        margin: { l: 40, r: 40, t: 40, b: 40 }
    };

    Plotly.newPlot(containerId, [trace], layout, PLOTLY_CONFIG);
}

// ============================================================================
// Panel 2: Volatility Smiles
// ============================================================================

function renderSmiles(containerId, smilesData, axis) {
    const container = document.getElementById(containerId);
    if (!container) return;

    if (!smilesData || !smilesData.slices || smilesData.slices.length === 0) {
        container.innerHTML = '<div class="empty-state">No smiles data available</div>';
        return;
    }

    const slices = smilesData.slices;
    const traces = [];

    // Color palette: short tenors warm, long tenors cool
    const colors = [
        '#ff6b6b', '#ff9c42', '#ffd93d', '#6bcf7f', '#4a90e2', '#9b59b6'
    ];

    slices.forEach((slice, idx) => {
        const color = colors[idx % colors.length];
        const dte = slice.dte_days;

        // Fitted curve (solid line)
        if (slice.fitted && slice.fitted.length > 0) {
            const fitted = slice.fitted.sort((a, b) => a.x - b.x);
            traces.push({
                x: fitted.map(p => p.x),
                y: fitted.map(p => p.iv),
                name: `DTE ${dte} (fit)`,
                mode: 'lines',
                line: { color, width: 2 },
                hovertemplate: 'LM: %{x:.3f}<br>IV: %{y:.2%}<extra></extra>'
            });
        }

        // Market points (scatter)
        if (slice.market && slice.market.length > 0) {
            traces.push({
                x: slice.market.map(p => p.x),
                y: slice.market.map(p => p.iv),
                name: `DTE ${dte} (market)`,
                mode: 'markers',
                marker: {
                    color,
                    size: 6,
                    opacity: 0.7,
                    symbol: 'circle'
                },
                hovertemplate: 'LM: %{x:.3f}<br>IV: %{y:.2%}<extra></extra>'
            });
        }
    });

    // ATM line
    traces.push({
        x: [-0.3, 0.3],
        y: [0, 0],
        mode: 'lines',
        line: { color: '#606080', width: 1, dash: 'dash' },
        name: 'ATM',
        hoverinfo: 'skip'
    });

    const layout = {
        ...PLOTLY_LAYOUT_DEFAULTS,
        title: { text: 'Volatility Smiles', font: { size: 14 } },
        xaxis: {
            title: axis === 'log_moneyness' ? 'Log-Moneyness' : axis,
            ...PLOTLY_LAYOUT_DEFAULTS.xaxis
        },
        yaxis: {
            title: 'Implied Vol',
            ...PLOTLY_LAYOUT_DEFAULTS.yaxis
        },
        margin: { l: 50, r: 20, t: 30, b: 40 }
    };

    Plotly.newPlot(containerId, traces, layout, PLOTLY_CONFIG);
}

// ============================================================================
// Panel 3: Market vs Fit Scatter
// ============================================================================

function renderMarketVsFit(containerId, smilesData) {
    const container = document.getElementById(containerId);
    if (!container) return;

    if (!smilesData || !smilesData.slices || smilesData.slices.length === 0) {
        container.innerHTML = '<div class="empty-state">No smiles data available</div>';
        return;
    }

    const slices = smilesData.slices;
    const traces = [];
    const colors = ['#ff6b6b', '#ff9c42', '#ffd93d', '#6bcf7f', '#4a90e2', '#9b59b6'];

    let allMarketIv = [];
    let allFittedIv = [];
    let allSizes = [];

    slices.forEach((slice, idx) => {
        const color = colors[idx % colors.length];
        const dte = slice.dte_days;

        if (!slice.market || !slice.fitted) return;

        // Map market points to fitted values
        const marketPoints = slice.market;
        const fittedMap = {};
        slice.fitted.forEach(p => {
            fittedMap[p.x.toFixed(3)] = p.iv;
        });

        marketPoints.forEach(mkt => {
            const fittedIv = fittedMap[mkt.x.toFixed(3)];
            if (fittedIv) {
                const lm = mkt.x;
                const distance = Math.abs(lm);
                const size = 6 + Math.max(0, 6 - distance * 10);

                allMarketIv.push(mkt.iv);
                allFittedIv.push(fittedIv);
                allSizes.push(size);
            }
        });
    });

    // Create trace with all points
    if (allMarketIv.length > 0) {
        traces.push({
            x: allMarketIv,
            y: allFittedIv,
            mode: 'markers',
            marker: {
                size: allSizes,
                color: allFittedIv,
                colorscale: 'Viridis',
                opacity: 0.6,
                line: { width: 0 }
            },
            name: 'Market vs Fit',
            hovertemplate: 'Market: %{x:.2%}<br>Fitted: %{y:.2%}<extra></extra>'
        });

        // Reference line (y = x)
        const minIv = Math.min(...allMarketIv.concat(allFittedIv));
        const maxIv = Math.max(...allMarketIv.concat(allFittedIv));
        traces.push({
            x: [minIv, maxIv],
            y: [minIv, maxIv],
            mode: 'lines',
            line: { color: '#606080', width: 1, dash: 'dash' },
            name: 'y = x',
            hoverinfo: 'skip'
        });

        // Compute R²
        const residuals = allFittedIv.map((f, i) => Math.pow(f - allMarketIv[i], 2));
        const ssRes = residuals.reduce((a, b) => a + b, 0);
        const meanMarket = allMarketIv.reduce((a, b) => a + b, 0) / allMarketIv.length;
        const ssTot = allMarketIv.map(m => Math.pow(m - meanMarket, 2))
            .reduce((a, b) => a + b, 0);
        const r2 = 1 - (ssRes / ssTot);
        const rmse = Math.sqrt(ssRes / allMarketIv.length);

        const layout = {
            ...PLOTLY_LAYOUT_DEFAULTS,
            title: {
                text: `Market vs Fit (R² = ${r2.toFixed(3)}, RMSE = ${(rmse * 100).toFixed(2)}%)`,
                font: { size: 13 }
            },
            xaxis: {
                title: 'Market IV',
                ...PLOTLY_LAYOUT_DEFAULTS.xaxis
            },
            yaxis: {
                title: 'Fitted IV',
                ...PLOTLY_LAYOUT_DEFAULTS.yaxis
            },
            margin: { l: 50, r: 20, t: 40, b: 40 }
        };

        Plotly.newPlot(containerId, traces, layout, PLOTLY_CONFIG);
    } else {
        container.innerHTML = '<div class="empty-state">No market data to compare</div>';
    }
}

// ============================================================================
// Panel 4: Term Structure
// ============================================================================

function renderTermStructure(containerId, smilesData, gridData) {
    const container = document.getElementById(containerId);
    if (!container) return;

    if (!smilesData || !smilesData.slices || smilesData.slices.length === 0) {
        container.innerHTML = '<div class="empty-state">No smiles data available</div>';
        return;
    }

    const slices = smilesData.slices.slice().sort((a, b) => a.dte_days - b.dte_days);
    const dtes = slices.map(s => s.dte_days);

    // ATM IV
    const atmIv = slices.map(slice => {
        // Find closest point to x=0
        const fitted = slice.fitted || [];
        const closest = fitted.reduce((prev, curr) =>
            (Math.abs(curr.x) < Math.abs(prev.x)) ? curr : prev, fitted[0]);
        return closest ? closest.iv : null;
    }).filter(v => v !== null);

    // 95% and 105% moneyness (log scale)
    const log095 = Math.log(0.95);
    const log105 = Math.log(1.05);

    const iv95 = slices.map(slice => {
        const fitted = slice.fitted || [];
        const closest = fitted.reduce((prev, curr) =>
            (Math.abs(curr.x - log095) < Math.abs(prev.x - log095)) ? curr : prev, fitted[0]);
        return closest ? closest.iv : null;
    }).filter(v => v !== null);

    const iv105 = slices.map(slice => {
        const fitted = slice.fitted || [];
        const closest = fitted.reduce((prev, curr) =>
            (Math.abs(curr.x - log105) < Math.abs(prev.x - log105)) ? curr : prev, fitted[0]);
        return closest ? closest.iv : null;
    }).filter(v => v !== null);

    const traces = [
        {
            x: dtes,
            y: atmIv,
            name: 'ATM',
            mode: 'lines+markers',
            line: { color: '#00d4ff', width: 2 },
            marker: { size: 6 }
        },
        {
            x: dtes,
            y: iv95,
            name: '95% Moneyness',
            mode: 'lines+markers',
            line: { color: '#ff6b6b', width: 2, dash: 'dash' },
            marker: { size: 5 }
        },
        {
            x: dtes,
            y: iv105,
            name: '105% Moneyness',
            mode: 'lines+markers',
            line: { color: '#6bcf7f', width: 2, dash: 'dash' },
            marker: { size: 5 }
        }
    ];

    const layout = {
        ...PLOTLY_LAYOUT_DEFAULTS,
        title: { text: 'Term Structure of Volatility', font: { size: 14 } },
        xaxis: {
            title: 'Days to Expiry',
            ...PLOTLY_LAYOUT_DEFAULTS.xaxis
        },
        yaxis: {
            title: 'Implied Vol',
            ...PLOTLY_LAYOUT_DEFAULTS.yaxis
        },
        margin: { l: 50, r: 20, t: 30, b: 40 }
    };

    Plotly.newPlot(containerId, traces, layout, PLOTLY_CONFIG);
}

// ============================================================================
// Panel 5: Diagnostics (HTML Table)
// ============================================================================

function renderDiagnostics(containerId, diagnosticsData) {
    const container = document.getElementById(containerId);
    if (!container) return;

    if (!diagnosticsData) {
        container.innerHTML = '<div class="empty-state">No diagnostics data</div>';
        return;
    }

    const { summary, fitted_params, slices, warnings } = diagnosticsData;

    let html = '';

    // Summary cards
    if (summary) {
        html += `
            <div class="diagnostics-card">
                <div class="diagnostics-card-item">
                    <span class="diagnostics-card-label">Method</span>
                    <span class="diagnostics-card-value">${summary.method.toUpperCase()}</span>
                </div>
                <div class="diagnostics-card-item">
                    <span class="diagnostics-card-label">Expiries</span>
                    <span class="diagnostics-card-value">${summary.n_expiries}</span>
                </div>
                <div class="diagnostics-card-item">
                    <span class="diagnostics-card-label">Accepted</span>
                    <span class="diagnostics-card-value">${summary.n_contracts_accepted}</span>
                </div>
                <div class="diagnostics-card-item">
                    <span class="diagnostics-card-label">Rejected</span>
                    <span class="diagnostics-card-value">${summary.n_contracts_rejected}</span>
                </div>
            </div>
        `;
    }

    // Per-slice table
    if (slices && slices.length > 0) {
        html += `
            <table class="diagnostics-table">
                <thead>
                    <tr>
                        <th>TTM</th>
                        <th>DTE</th>
                        <th>Contracts</th>
                        <th>Solved</th>
                        <th>Failed</th>
                        <th>RMSE</th>
                        <th>Arb</th>
                    </tr>
                </thead>
                <tbody>
        `;
        slices.forEach(slice => {
            const arbStatus = slice.arbitrage_passed ? '✓' : '✗';
            const arbColor = slice.arbitrage_passed ? '#22c55e' : '#ef4444';
            html += `
                <tr>
                    <td>${slice.ttm.toFixed(4)}</td>
                    <td>${slice.n_contracts || '—'}</td>
                    <td>${slice.n_contracts || '—'}</td>
                    <td>${slice.n_solved || '—'}</td>
                    <td>${slice.n_failed || '—'}</td>
                    <td>${(slice.fit_rmse * 100).toFixed(2)}%</td>
                    <td style="color: ${arbColor}; font-weight: 700;">${arbStatus}</td>
                </tr>
            `;
        });
        html += '</tbody></table>';
    }

    // Fitted params
    if (fitted_params && fitted_params.length > 0) {
        html += `
            <table class="diagnostics-table">
                <thead>
                    <tr>
                        <th>TTM</th>
                        <th>a</th>
                        <th>b</th>
                        <th>ρ</th>
                        <th>m</th>
                        <th>σ</th>
                    </tr>
                </thead>
                <tbody>
        `;
        fitted_params.forEach(fp => {
            const p = fp.params;
            html += `
                <tr>
                    <td>${fp.ttm.toFixed(4)}</td>
                    <td>${p.a.toFixed(4)}</td>
                    <td>${p.b.toFixed(4)}</td>
                    <td>${p.rho.toFixed(3)}</td>
                    <td>${p.m.toFixed(3)}</td>
                    <td>${p.sigma.toFixed(3)}</td>
                </tr>
            `;
        });
        html += '</tbody></table>';
    }

    // Warnings
    if (warnings && warnings.length > 0) {
        html += `
            <div class="diagnostics-warnings">
                <div class="diagnostics-warnings-title">Warnings</div>
                <ul class="diagnostics-warnings-list">
        `;
        warnings.forEach(w => {
            html += `<li>${w}</li>`;
        });
        html += '</ul></div>';
    }

    container.innerHTML = html;
}

// ============================================================================
// Panel 6: Rejections (Bar + Pie Charts)
// ============================================================================

function renderRejections(containerId, diagnosticsData) {
    const container = document.getElementById(containerId);
    if (!container) return;

    if (!diagnosticsData || !diagnosticsData.rejections) {
        container.innerHTML = '<div class="empty-state">No rejection data</div>';
        return;
    }

    const { rejections } = diagnosticsData;

    // Sort rejection reasons by count
    const reasons = Object.entries(rejections.by_reason)
        .map(([reason, count]) => ({ reason: reason.replace(/_/g, ' '), count }))
        .sort((a, b) => b.count - a.count);

    // Bar trace
    const barTrace = {
        x: reasons.map(r => r.count),
        y: reasons.map(r => r.reason),
        type: 'bar',
        orientation: 'h',
        marker: {
            color: reasons.map((_, i) => [
                '#ff6b6b', '#ff9c42', '#ffd93d', '#6bcf7f', '#4a90e2'
            ][i % 5]),
            line: { color: '#2d3561', width: 1 }
        },
        text: reasons.map(r => r.count),
        textposition: 'outside',
        hovertemplate: '%{y}: %{x}<extra></extra>'
    };

    // Pie trace (accepted vs rejected)
    const pieTrace = {
        values: [rejections.accepted, rejections.rejected],
        labels: ['Accepted', 'Rejected'],
        type: 'pie',
        hole: 0.4,
        marker: {
            colors: ['#22c55e', '#ef4444'],
            line: { color: '#16213e', width: 2 }
        },
        hovertemplate: '%{label}: %{value} (%{percent})<extra></extra>'
    };

    const data = [barTrace];

    const layout = {
        ...PLOTLY_LAYOUT_DEFAULTS,
        title: { text: 'Quote Rejections', font: { size: 14 } },
        xaxis: {
            title: 'Count',
            ...PLOTLY_LAYOUT_DEFAULTS.xaxis
        },
        yaxis: {
            title: '',
            ...PLOTLY_LAYOUT_DEFAULTS.yaxis
        },
        margin: { l: 150, r: 20, t: 30, b: 40 }
    };

    Plotly.newPlot(containerId, data, layout, PLOTLY_CONFIG);

    // Create pie chart in a separate container if space allows
    // For now, just the bar chart
}

// ============================================================================
// Export for dashboard.js
// ============================================================================

window.DashboardPanels = {
    renderSurface,
    renderSmiles,
    renderMarketVsFit,
    renderTermStructure,
    renderDiagnostics,
    renderRejections
};
