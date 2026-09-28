// Guard: the "no money arithmetic in the browser" lint rules (PRD #2560 D12,
// `eslint.config.mjs`) must actually fire. A lint rule nobody has seen fail
// is a check that cannot fire; this lints each known bypass shape through the
// real config and fails if any shape passes clean, if a money surface falls
// outside the rule's scope, or if the rule starts flagging honest code.

const path = require("node:path");
const { ESLint } = require("eslint");

const frontendRoot = path.join(__dirname, "..");
const MONEY_RULES = new Set(["no-restricted-syntax", "no-restricted-globals", "no-implicit-coercion"]);

// Layer 1: anywhere in the app, arithmetic or coercion on a money value.
const ANYWHERE = "src/app/components/example/plain.component";
// Layer 2: a money surface by convention, where any coercion is refused.
const MONEY_SURFACES = [
  "src/app/components/broker/money-bar/money-bar.component",
  "src/app/components/broker/v2-panel/lib/account-money-state",
  "src/app/components/broker/deployment-budget/deployment-budget.component",
  "src/app/components/broker/broker-deploy-page/deploy-budget-review.component",
  "src/app/components/brokers/alpaca-workspace/alpaca-account-workspace.component",
  "src/app/components/brokers/alpaca-desk/alpaca-lane-card.component",
  "src/app/components/brokers/alpaca-desk/alpaca-account-list-page.component",
  // Surfaces slices 2–4 will add fall under the convention by name or place.
  "src/app/components/broker/v2-panel/bot-money/bot-money.component",
  "src/app/components/broker/broker-deploy-page/deploy-money-step.component",
  "src/app/components/brokers/alpaca-workspace/alpaca-home.component",
];

/** Shapes that must be refused everywhere. */
const REFUSED_ANYWHERE = [
  ["ts", "export const a = +view['free_to_deploy_usd'];"],
  ["ts", "export const a = +view?.free_to_deploy_usd;"],
  ["ts", "export const a = view.share_bps / 100;"],
  ["ts", "export function f(s: { share_bps: number }) { let total = 0; total += s.share_bps; return total; }"],
  ["ts", "export function f({ share_bps }: { share_bps: number }) { return share_bps * 2; }"],
  ["ts", "export const a = Number(budget.parts!.in_shares_usd);"],
  ["ts", "export const a = parseFloat(view.cash_usd);"],
  ["ts", "export const a = Math.round(segment.share_bps);"],
  ["ts", "export const a = segments.reduce((sum, s) => sum + s.share_bps, 0);"],
  ["ts", "export const a = (view.total_usd as unknown as number) - 1;"],
  ["html", "<p>{{ +view.cash_usd }}</p>"],
  ["html", "<p>{{ view.cash_usd + view['in_bots_usd'] }}</p>"],
  ["html", "<p>{{ segment.share_bps / 100 }}</p>"],
  ["html", "<p>{{ -(budget.parts!.free_usd) }}</p>"],
];

/** Shapes a plain name hides from layer 1; refused on a money surface. */
const REFUSED_ON_MONEY_SURFACES = [
  ["ts", "const f = view.free_to_deploy_usd; export const a = +f;"],
  ["ts", "const { free_to_deploy_usd: f } = view; export const a = Number(f);"],
  ["ts", "export const a = parseInt(amount, 10);"],
  ["ts", "export const a = -amount;"],
  ["ts", "export const a = '' + amount;"],
  ["ts", "export const a = globalThis.parseFloat(amount);"],
  ["html", "@let f = view.free_to_deploy_usd; <p>{{ +f }}</p>"],
  ["html", "@let w = segment.share_bps; <p>{{ w / 100 }}</p>"],
];

/** Honest code that must stay clean. */
const ALLOWED = [
  [MONEY_SURFACES[0], "html", "<p>{{ segment.amount_usd | currency: 'USD' }}</p>"],
  [MONEY_SURFACES[0], "html", "@if (segment.share_bps > 0) { <span [style.flex-grow]=\"segment.share_bps\"></span> }"],
  [MONEY_SURFACES[0], "ts", "export const offset = -1;"],
  [MONEY_SURFACES[5], "ts", "export const free = (view: { free_to_deploy_usd: string }) => view.free_to_deploy_usd;"],
  [ANYWHERE, "ts", "export const quantity = Number(input);"],
  [ANYWHERE, "html", "<p>{{ count - 1 }}</p>"],
];

async function moneyMessages(eslint, file, extension, code) {
  const [result] = await eslint.lintText(code, { filePath: path.join(frontendRoot, `${file}.${extension}`) });
  return result.messages.filter((message) => MONEY_RULES.has(message.ruleId) || message.fatal);
}

async function main() {
  const eslint = new ESLint({ cwd: frontendRoot });
  const failures = [];

  const expectRefused = async (file, extension, code) => {
    const messages = await moneyMessages(eslint, file, extension, code);
    if (messages.length === 0 || messages.some((message) => message.fatal)) {
      failures.push(`not refused in ${file}.${extension}: ${code}`);
    }
  };

  for (const [extension, code] of REFUSED_ANYWHERE) {
    await expectRefused(ANYWHERE, extension, code);
  }
  for (const file of MONEY_SURFACES) {
    for (const [extension, code] of REFUSED_ON_MONEY_SURFACES) {
      await expectRefused(file, extension, code);
    }
  }
  for (const [file, extension, code] of ALLOWED) {
    const messages = await moneyMessages(eslint, file, extension, code);
    if (messages.length > 0) {
      failures.push(`honest code refused in ${file}.${extension}: ${code} (${messages.map((m) => m.message).join("; ")})`);
    }
  }

  if (failures.length > 0) {
    console.error("Money arithmetic lint guard failed:\n" + failures.map((line) => `  - ${line}`).join("\n"));
    process.exit(1);
  }
  console.log(
    `money arithmetic lint guard passed (${REFUSED_ANYWHERE.length} shapes everywhere, `
      + `${REFUSED_ON_MONEY_SURFACES.length} on ${MONEY_SURFACES.length} money surfaces, ${ALLOWED.length} allowed)`,
  );
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
