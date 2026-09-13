// Guard: the chart-series color token registry in
// `src/app/shared/trading-chart/chart-series-color-tokens.ts` and the SCSS
// literals in `src/app/styles/_tokens.scss` must stay in lockstep. The TS hex
// literals power the WCAG contrast spec; the SCSS vars power the runtime CSS
// custom properties. This script fails when either side drifts.

const fs = require("node:fs");
const path = require("node:path");

const frontendRoot = path.join(__dirname, "..");
const scssPath = path.join(frontendRoot, "src/app/styles/_tokens.scss");
const tsPath = path.join(
  frontendRoot,
  "src/app/shared/trading-chart/chart-series-color-tokens.ts",
);

const scss = fs.readFileSync(scssPath, "utf8");
const ts = fs.readFileSync(tsPath, "utf8");

const scssVars = new Map();
for (const m of scss.matchAll(/\$chart-series-([\w-]+):\s*(#[0-9a-fA-F]{6});/g)) {
  scssVars.set(m[1], m[2].toLowerCase());
}
const scssSurface = scss.match(/\$bg-surface:\s*(#[0-9a-fA-F]{6});/);

const tsEntries = new Map();
for (const m of ts.matchAll(/id:\s*'(series-[\w-]+)'[\s\S]{0,200}?hex:\s*'(#[0-9a-fA-F]{6})'/g)) {
  tsEntries.set(m[1], m[2].toLowerCase());
}
const tsSurface = ts.match(/CHART_SERIES_SURFACE_HEX\s*=\s*'(#[0-9a-fA-F]{6})'/);

const failures = [];

if (scssVars.size === 0) failures.push("no $chart-series-* SCSS variables found");
if (tsEntries.size === 0) failures.push("no token registry entries found in TS");

for (const [id, hex] of tsEntries) {
  const key = id.replace(/^series-/, "");
  if (!scssVars.has(key)) {
    failures.push(`token ${id} has no $chart-series-${key} SCSS variable`);
  } else if (scssVars.get(key) !== hex) {
    failures.push(`token ${id}: TS hex ${hex} != SCSS hex ${scssVars.get(key)}`);
  }
}
for (const key of scssVars.keys()) {
  if (!tsEntries.has(`series-${key}`)) {
    failures.push(`$chart-series-${key} has no TS registry entry`);
  }
}

if (!scssSurface) {
  failures.push("no $bg-surface SCSS variable found");
} else if (!tsSurface) {
  failures.push("no CHART_SERIES_SURFACE_HEX in TS");
} else if (scssSurface[1].toLowerCase() !== tsSurface[1].toLowerCase()) {
  failures.push(
    `surface mismatch: TS ${tsSurface[1]} != SCSS ${scssSurface[1]}`,
  );
}

for (const [key] of scssVars) {
  if (!new RegExp(`--chart-series-${key}:\\s*#\\{\\$chart-series-${key}\\};`).test(scss)) {
    failures.push(`--chart-series-${key} custom property is not derived from the SCSS var`);
  }
}

if (failures.length > 0) {
  console.error("chart series color token guard failed:");
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}

console.log(
  `chart series color token guard passed (${tsEntries.size} tokens, surface ${tsSurface && tsSurface[1]})`,
);
