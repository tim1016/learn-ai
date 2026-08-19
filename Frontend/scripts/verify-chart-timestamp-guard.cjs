const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const CHART_COMPONENT_PATH = path.resolve(
  __dirname,
  '../src/app/components/broker/v2-panel/dual-pane-chart/dual-pane-chart.component.ts',
);

const source = fs.readFileSync(CHART_COMPONENT_PATH, 'utf8');

const bannedPatterns = [
  [/Intl\.DateTimeFormat/, 'Intl.DateTimeFormat'],
  [/\.toLocaleString\(|\.toLocaleDateString\(|\.toLocaleTimeString\(/, 'toLocale*String()'],
  [/\bDatePipe\b/, 'DatePipe'],
];

for (const [pattern, label] of bannedPatterns) {
  assert.equal(
    pattern.test(source),
    false,
    `dual-pane-chart.component.ts must not format timestamps directly via ${label} — `
      + 'delegate to shared/timestamp/timestamp-display.ts (temporal-rigor.md: "Display").',
  );
}

assert.match(
  source,
  /from ['"].*shared\/timestamp\/timestamp-display['"]/,
  'dual-pane-chart.component.ts must import the shared timestamp display core',
);

process.stdout.write('chart timestamp guard ok\n');
