/**
 * IV Dashboard — State Management & API Integration
 *
 * State machine for loading, building, and exporting IV surfaces.
 * Falls back to sample data when server is unavailable.
 */

// ============================================================================
// Global State
// ============================================================================

const Dashboard = {
    // API base URL (adjust as needed)
    apiBaseUrl: window.location.origin === 'file://'
        ? 'http://localhost:8000/api'
        : `${window.location.origin}/api`,

    // Current state
    state: {
        surfaceId: null,
        ticker: 'SPY',
        date: new Date().toISOString().split('T')[0],
        method: 'svi',
        axis: 'log_moneyness',
        mode: 'auto',
        data: null,
        loading: false,
        lastError: null
    },

    // ========================================================================
    // Initialization
    // ========================================================================

    async init() {
        console.log('[Dashboard] Initializing...');

        // Set default date to today
        const dateInput = document.getElementById('date-input');
        dateInput.value = this.state.date;

        // Load sample data initially
        this.loadSampleData();
        this.updateAllPanels();
        this.setStatus('Loaded sample data', 'info');

        // Attach event listeners
        this.attachEventListeners();

        console.log('[Dashboard] Ready');
    },

    // ========================================================================
    // Event Listeners
    // ========================================================================

    attachEventListeners() {
        // Build button
        document.getElementById('build-btn').addEventListener('click', () => {
            this.buildSurface();
        });

        // CSV upload
        document.getElementById('csv-btn').addEventListener('click', () => {
            document.getElementById('csv-upload').click();
        });
        document.getElementById('csv-upload').addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                this.loadFromCsv(e.target.files[0]);
            }
        });

        // Export dropdown
        document.getElementById('export-dropdown').addEventListener('change', (e) => {
            if (e.target.value) {
                this.exportSurface(e.target.value);
                e.target.value = '';
            }
        });

        // Control changes
        document.getElementById('ticker-input').addEventListener('change', (e) => {
            this.state.ticker = e.target.value.toUpperCase() || 'SPY';
        });

        document.getElementById('date-input').addEventListener('change', (e) => {
            this.state.date = e.target.value;
        });

        document.getElementById('method-dropdown').addEventListener('change', (e) => {
            this.state.method = e.target.value;
        });

        document.getElementById('axis-dropdown').addEventListener('change', (e) => {
            this.state.axis = e.target.value;
            this.updateAllPanels();
        });

        document.getElementById('mode-dropdown').addEventListener('change', (e) => {
            this.state.mode = e.target.value;
        });
    },

    // ========================================================================
    // Data Loading
    // ========================================================================

    loadSampleData() {
        if (!window.SAMPLE_DATA) {
            console.error('[Dashboard] Sample data not available');
            return;
        }

        this.state.data = window.SAMPLE_DATA;
        this.state.surfaceId = window.SAMPLE_DATA.summary.surface_id;
        this.updateHealthBadge(window.SAMPLE_DATA.summary.health_score);
    },

    async buildSurface() {
        const { ticker, date, method, mode } = this.state;

        console.log('[Dashboard] Building surface:', { ticker, date, method, mode });
        this.setStatus('Building surface...', 'info');
        this.state.loading = true;

        try {
            // Step 1: Build or fetch surface
            const buildUrl = `${this.apiBaseUrl}/volatility/surface/build-from-ticker`;
            const buildParams = new URLSearchParams({
                ticker,
                date: date || new Date().toISOString().split('T')[0],
                method,
                mode: mode === 'cached' ? 'prefer_cached' : mode
            });

            const buildResp = await fetch(`${buildUrl}?${buildParams}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            });

            if (!buildResp.ok) {
                throw new Error(`Build failed: ${buildResp.status} ${buildResp.statusText}`);
            }

            const buildData = await buildResp.json();
            this.state.surfaceId = buildData.surface_id;

            // Step 2: Fetch grid
            const gridUrl = `${this.apiBaseUrl}/volatility/surface/${this.state.surfaceId}/grid`;
            const gridResp = await fetch(gridUrl);
            if (!gridResp.ok) throw new Error(`Grid fetch failed: ${gridResp.status}`);
            const gridData = await gridResp.json();

            // Step 3: Fetch smiles
            const smilesUrl = `${this.apiBaseUrl}/volatility/surface/${this.state.surfaceId}/smiles`;
            const smilesResp = await fetch(smilesUrl);
            if (!smilesResp.ok) throw new Error(`Smiles fetch failed: ${smilesResp.status}`);
            const smilesData = await smilesResp.json();

            // Step 4: Fetch diagnostics
            const diagUrl = `${this.apiBaseUrl}/volatility/surface/${this.state.surfaceId}/diagnostics`;
            const diagResp = await fetch(diagUrl);
            if (!diagResp.ok) throw new Error(`Diagnostics fetch failed: ${diagResp.status}`);
            const diagnosticsData = await diagResp.json();

            // Step 5: Assemble state
            this.state.data = {
                summary: buildData,
                grid: gridData,
                smiles: smilesData,
                diagnostics: diagnosticsData
            };

            this.updateHealthBadge(buildData.health_score);
            this.updateAllPanels();

            this.setStatus(
                `Surface built for ${ticker} on ${date}: ${buildData.n_contracts_accepted} accepted, ` +
                `${buildData.n_contracts_rejected} rejected`,
                'success'
            );
        } catch (error) {
            console.error('[Dashboard] Build error:', error);
            this.state.lastError = error.message;

            // Graceful fallback to sample data
            console.log('[Dashboard] Falling back to sample data');
            this.loadSampleData();
            this.updateAllPanels();

            this.setStatus(
                `Server unavailable: ${error.message}. Using sample data.`,
                'error'
            );
        } finally {
            this.state.loading = false;
        }
    },

    async loadFromCsv(file) {
        console.log('[Dashboard] Loading CSV:', file.name);
        this.setStatus('Uploading CSV...', 'info');
        this.state.loading = true;

        try {
            const formData = new FormData();
            formData.append('file', file);

            const uploadUrl = `${this.apiBaseUrl}/volatility/surface/build-from-csv`;
            const uploadResp = await fetch(uploadUrl, {
                method: 'POST',
                body: formData
            });

            if (!uploadResp.ok) {
                throw new Error(`CSV upload failed: ${uploadResp.status}`);
            }

            const buildData = await uploadResp.json();
            this.state.surfaceId = buildData.surface_id;

            // Fetch remaining data
            const [gridData, smilesData, diagnosticsData] = await Promise.all([
                fetch(`${this.apiBaseUrl}/volatility/surface/${this.state.surfaceId}/grid`)
                    .then(r => r.json()),
                fetch(`${this.apiBaseUrl}/volatility/surface/${this.state.surfaceId}/smiles`)
                    .then(r => r.json()),
                fetch(`${this.apiBaseUrl}/volatility/surface/${this.state.surfaceId}/diagnostics`)
                    .then(r => r.json())
            ]);

            this.state.data = {
                summary: buildData,
                grid: gridData,
                smiles: smilesData,
                diagnostics: diagnosticsData
            };

            this.updateHealthBadge(buildData.health_score);
            this.updateAllPanels();

            this.setStatus(
                `CSV loaded: ${buildData.n_contracts_accepted} contracts accepted`,
                'success'
            );
        } catch (error) {
            console.error('[Dashboard] CSV load error:', error);
            this.setStatus(`CSV load failed: ${error.message}`, 'error');
        } finally {
            this.state.loading = false;
        }
    },

    // ========================================================================
    // Data Fetching
    // ========================================================================

    async fetchGrid(nStrikes = 30) {
        if (!this.state.surfaceId) return null;
        const url = `${this.apiBaseUrl}/volatility/surface/${this.state.surfaceId}/grid?n_strikes=${nStrikes}`;
        return fetch(url).then(r => r.json()).catch(e => {
            console.error('Grid fetch error:', e);
            return null;
        });
    },

    async fetchSmiles() {
        if (!this.state.surfaceId) return null;
        const url = `${this.apiBaseUrl}/volatility/surface/${this.state.surfaceId}/smiles`;
        return fetch(url).then(r => r.json()).catch(e => {
            console.error('Smiles fetch error:', e);
            return null;
        });
    },

    async fetchDiagnostics() {
        if (!this.state.surfaceId) return null;
        const url = `${this.apiBaseUrl}/volatility/surface/${this.state.surfaceId}/diagnostics`;
        return fetch(url).then(r => r.json()).catch(e => {
            console.error('Diagnostics fetch error:', e);
            return null;
        });
    },

    // ========================================================================
    // Export
    // ========================================================================

    async exportSurface(format) {
        if (!this.state.surfaceId) {
            this.setStatus('No surface to export', 'error');
            return;
        }

        console.log('[Dashboard] Exporting as', format);
        this.setStatus(`Exporting as ${format.toUpperCase()}...`, 'info');

        try {
            const url = `${this.apiBaseUrl}/volatility/surface/${this.state.surfaceId}/export/${format}`;
            const resp = await fetch(url);

            if (!resp.ok) {
                throw new Error(`Export failed: ${resp.status}`);
            }

            // Determine filename
            const ticker = this.state.data?.summary?.ticker || 'surface';
            const date = this.state.data?.summary?.date || new Date().toISOString().split('T')[0];
            const filename = `${ticker}_${date}.${format}`;

            // Download
            const blob = await resp.blob();
            const link = document.createElement('a');
            link.href = URL.createObjectURL(blob);
            link.download = filename;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);

            this.setStatus(`Exported as ${filename}`, 'success');
        } catch (error) {
            console.error('[Dashboard] Export error:', error);
            this.setStatus(`Export failed: ${error.message}`, 'error');
        }
    },

    // ========================================================================
    // Panel Rendering
    // ========================================================================

    updateAllPanels() {
        if (!this.state.data) {
            console.warn('[Dashboard] No data to render');
            return;
        }

        const { grid, smiles, diagnostics } = this.state.data;
        const { axis } = this.state;

        console.log('[Dashboard] Rendering all panels...');

        // Panel 1: Surface
        if (grid) {
            DashboardPanels.renderSurface('surface-chart', grid, axis);
        }

        // Panel 2: Smiles
        if (smiles) {
            DashboardPanels.renderSmiles('smiles-chart', smiles, axis);
        }

        // Panel 3: Market vs Fit
        if (smiles) {
            DashboardPanels.renderMarketVsFit('scatter-chart', smiles);
        }

        // Panel 4: Term Structure
        if (smiles && grid) {
            DashboardPanels.renderTermStructure('term-chart', smiles, grid);
        }

        // Panel 5: Diagnostics
        if (diagnostics) {
            DashboardPanels.renderDiagnostics('diagnostics-content', diagnostics);
        }

        // Panel 6: Rejections
        if (diagnostics) {
            DashboardPanels.renderRejections('rejections-chart', diagnostics);
        }
    },

    // ========================================================================
    // UI Updates
    // ========================================================================

    setStatus(message, type = 'info') {
        const statusEl = document.getElementById('status-message');
        if (!statusEl) return;

        statusEl.textContent = message;
        statusEl.className = type;

        console.log(`[Dashboard] ${type.toUpperCase()}: ${message}`);
    },

    updateHealthBadge(score) {
        const badge = document.getElementById('health-badge');
        if (!badge) return;

        let className = 'score-low';
        if (score >= 80) {
            className = 'score-high';
        } else if (score >= 60) {
            className = 'score-medium';
        }

        badge.className = `health-badge ${className}`;
        badge.querySelector('.health-score').textContent = Math.round(score);
    }
};

// ============================================================================
// Page Lifecycle
// ============================================================================

document.addEventListener('DOMContentLoaded', () => {
    Dashboard.init();
});

// Graceful error handling
window.addEventListener('error', (event) => {
    console.error('[Dashboard] Global error:', event.error);
    Dashboard.setStatus(`Error: ${event.error?.message || 'Unknown error'}`, 'error');
});

window.addEventListener('unhandledrejection', (event) => {
    console.error('[Dashboard] Unhandled rejection:', event.reason);
    Dashboard.setStatus(`Error: ${event.reason?.message || 'Async error'}`, 'error');
});
