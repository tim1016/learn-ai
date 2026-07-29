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
const canonicalAuthorityMarker = "AUTHORITY STATUS: CANONICAL — SOLE CURRENT AUTHORITY";

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

const canonicalManualText = fs.readFileSync(canonicalOperatorManual, "utf8");
if (!canonicalManualText.includes(canonicalAuthorityMarker)) {
  throw new Error("The canonical Bot Control manual must declare its sole-current-authority status.");
}

function markdownFiles(root) {
  return fs.readdirSync(root, { withFileTypes: true }).flatMap((entry) => {
    const entryPath = path.join(root, entry.name);
    if (entry.isDirectory()) return markdownFiles(entryPath);
    return entry.isFile() && entry.name.endsWith(".md") ? [entryPath] : [];
  });
}

const duplicateAuthorityMarkers = markdownFiles(path.resolve(frontendRoot, "../docs"))
  .filter((file) => file !== canonicalOperatorManual)
  .filter((file) => fs.readFileSync(file, "utf8").includes(canonicalAuthorityMarker));
if (duplicateAuthorityMarkers.length) {
  throw new Error(
    "A second document claims sole current Bot Control authority: " +
      duplicateAuthorityMarkers.map((file) => path.relative(frontendRoot, file)).join(", "),
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
