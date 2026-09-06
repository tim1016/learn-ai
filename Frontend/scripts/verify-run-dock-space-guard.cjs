const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

// The run dock is position:fixed at the viewport bottom and publishes its
// current height (36px collapsed, 320px expanded) as --run-dock-height on
// :root. Every page that hosts <app-run-dock> must reserve that height, or
// the last controls hide under the expanded dock (#1974).

const HOSTS = [
  ['src/app/components/strategy-lab/strategy-lab.component.scss', 'Strategy Lab'],
  ['src/app/components/data-lab/data-lab.component.scss', 'Data Lab'],
];

for (const [relativePath, label] of HOSTS) {
  const source = fs.readFileSync(path.resolve(__dirname, '..', relativePath), 'utf8');
  assert.match(
    source,
    /var\(--run-dock-height/,
    `${label} (${relativePath}) hosts the run dock and must reserve var(--run-dock-height) instead of a fixed strip height.`,
  );
}

const dock = fs.readFileSync(path.resolve(__dirname, '../src/app/shared/run-dock/run-dock.component.ts'), 'utf8');
assert.match(dock, /setProperty\('--run-dock-height'/, 'run-dock.component.ts must publish --run-dock-height for its hosts.');

process.stdout.write('run dock space guard ok\n');
