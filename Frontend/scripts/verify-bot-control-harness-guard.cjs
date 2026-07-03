const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const specRoot = path.join(
  __dirname,
  '..',
  'src',
  'app',
  'components',
  'broker',
  'bot-control',
);

const allowMarker = 'bot-control-allow-configure-live-runs';
const offenders = [];

function specFilesUnder(directory) {
  const entries = fs.readdirSync(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const childPath = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      files.push(...specFilesUnder(childPath));
    } else if (entry.isFile() && entry.name.endsWith('.spec.ts')) {
      files.push(childPath);
    }
  }
  return files.sort();
}

for (const specPath of specFilesUnder(specRoot)) {
  const lines = fs.readFileSync(specPath, 'utf8').split(/\r?\n/);

  for (const [index, line] of lines.entries()) {
    if (!/\bconfigureLiveRuns\b/.test(line)) continue;

    const markerWindow = lines.slice(Math.max(0, index - 2), index + 1).join('\n');
    if (!markerWindow.includes(allowMarker)) {
      offenders.push(`${specPath}:${index + 1}: ${line.trim()}`);
    }
  }
}

assert.equal(
  offenders.length,
  0,
  [
    'Bot Control specs must use typed read/mutation harness options for ordinary LiveRunsService setup.',
    `Add ${allowMarker} immediately above an intentional bespoke configureLiveRuns escape hatch.`,
    ...offenders,
  ].join('\n'),
);
