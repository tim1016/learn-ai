const fs = require("node:fs");
const { spawnSync } = require("node:child_process");
const path = require("node:path");

const frontendRoot = path.join(__dirname, "..");
const canonicalOperatorManual = path.resolve(
  frontendRoot,
  "../docs/bot-control-operator-manual.md",
);
const bundledOperatorManual = path.join(
  frontendRoot,
  "src/assets/docs/bot-control-operator-manual.md",
);

if (
  fs.existsSync(canonicalOperatorManual) &&
  fs.readFileSync(canonicalOperatorManual, "utf8") !==
    fs.readFileSync(bundledOperatorManual, "utf8")
) {
  throw new Error(
    "Bundled Bot Control manual is stale. Copy docs/bot-control-operator-manual.md " +
      "to Frontend/src/assets/docs/bot-control-operator-manual.md.",
  );
}

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
