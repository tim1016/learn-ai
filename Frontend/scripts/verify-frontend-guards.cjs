const { spawnSync } = require("node:child_process");
const path = require("node:path");

const frontendRoot = path.join(__dirname, "..");
const guardCommands = [
  [
    "Bot Control harness guard",
    "node",
    ["scripts/verify-bot-control-harness-guard.cjs"],
  ],
  ["proxy control guard", "node", ["scripts/verify-proxy-control-guard.cjs"]],
  [
    "live instance literal contract guard",
    "node",
    ["scripts/verify-live-instance-literal-contract.cjs"],
  ],
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
