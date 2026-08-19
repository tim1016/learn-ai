const { spawnSync } = require("node:child_process");
const path = require("node:path");

const frontendRoot = path.join(__dirname, "..");

const guardCommands = [
  ["proxy control guard", "node", ["scripts/verify-proxy-control-guard.cjs"]],
  ["chart timestamp guard", "node", ["scripts/verify-chart-timestamp-guard.cjs"]],
];

for (const [label, command, args] of guardCommands) {
  const result = spawnSync(command, args, {
    cwd: frontendRoot,
    stdio: "inherit",
  });
  if (result.error) {
    throw new Error(`${label} failed to launch: ${result.error.message}`);
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}
