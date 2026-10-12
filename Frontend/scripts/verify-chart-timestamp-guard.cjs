const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

// Every chart component that draws a time on screen. Each one is held to the same rule below.
const CHART_COMPONENT_PATHS = [
  '../src/app/components/broker/v2-panel/dual-pane-chart/dual-pane-chart.component.ts',
  '../src/app/components/options-backtest/run-detail/session/timeline-chart.component.ts',
].map((relative) => path.resolve(__dirname, relative));

const bannedPatterns = [
  [/Intl\.DateTimeFormat/, 'Intl.DateTimeFormat'],
  [/\.toLocaleString\(|\.toLocaleDateString\(|\.toLocaleTimeString\(/, 'toLocale*String()'],
  [/\bDatePipe\b/, 'DatePipe'],
];

for (const chartPath of CHART_COMPONENT_PATHS) {
  const name = path.basename(chartPath);
  const source = fs.readFileSync(chartPath, 'utf8');

  for (const [pattern, label] of bannedPatterns) {
    assert.equal(
      pattern.test(source),
      false,
      `${name} must not format timestamps directly via ${label} — `
        + 'delegate to shared/timestamp/timestamp-display.ts (temporal-rigor.md: "Display").',
    );
  }

  assert.match(
    source,
    /from ['"].*shared\/timestamp\/timestamp-display['"]/,
    `${name} must import the shared timestamp display core`,
  );
}

process.stdout.write('chart timestamp guard ok\n');
