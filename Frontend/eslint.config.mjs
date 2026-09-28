import eslint from "@eslint/js";
import tseslint from "typescript-eslint";
import angular from "angular-eslint";
import unusedImports from "eslint-plugin-unused-imports";
import noMoneyAliasArithmetic from "./eslint-rules/no-money-alias-arithmetic.mjs";

// ---------------------------------------------------------------------------
// No money arithmetic in the browser (PRD #2560 D12).
//
// Python authors every dollar string and every slice width; the browser only
// renders them. Two layers enforce that, and
// `scripts/verify-money-arithmetic-lint-guard.cjs` proves each one fires.
//
// 1. Everywhere (every non-spec `.ts` and template): no arithmetic, unary
//    sign, `++`/`--`, compound assignment, `Number()`/`parseFloat`/`parseInt`/
//    `BigInt`/`Math.*` on a money value — a `*_usd` or `*_bps` member (dot,
//    optional-chain or bracket read, cast or not) or a binding with that name
//    (a destructured `share_bps`). This layer needs no scope: every backend
//    money field is named `*_usd` / `*_bps`, so a new file reading
//    `DeploymentBudgetView.parts` is covered the day it is written.
//
// 2. Money surfaces: a value can be aliased to a plain name first
//    (`const f = view.free_to_deploy_usd; +f`), so on the files that render
//    money any numeric coercion at all is banned — `Number`, `parseFloat`,
//    `parseInt`, `BigInt`, `Math`, unary `+`/`-` on a non-literal and
//    implicit coercion (`no-implicit-coercion`) — the scope-aware
//    `learn-ai/no-money-alias-arithmetic` rule refuses arithmetic on an
//    alias of a money value, and their templates may do no arithmetic
//    whatever and may not hand any value to a pipe that formats numbers
//    (`currency`, `number`, `percent`): Angular's numeric pipes parse their
//    input before formatting it, so `segment.amount_usd | currency` re-formats
//    — and can re-round or reject — the string Python authored. Render
//    authored money strings with `authoredUsd` (a pure string formatter) or
//    as given. A money surface is, by convention:
//      - any file or directory whose name contains `money` or `budget` (the
//        money bar, `account-money-state.ts`, the bot page's budget card,
//        Deploy's budget/Money step — name a new money component that way);
//      - the account workspace directory `components/brokers/alpaca-workspace/`
//        (its header and the pages built there);
//      - the account's Home in `components/brokers/alpaca-home/`: its page,
//        bot rows, Wall tiles and Finished results (its bar is `home-money`);
//      - the Accounts page and its account cards.
// ---------------------------------------------------------------------------
const MONEY_NAME = "/_(usd|bps)$/";
const MONEY_MESSAGE =
  "PRD #2560 D12: the browser does no money arithmetic. Render the Python-authored *_usd string or *_bps width as given.";

/** A TypeScript operand that is a money value: `x_usd`, `a.x_usd`,
 * `a?.x_usd`, `a['x_usd']`, and any of those behind up to two wrappers
 * (`a.x_usd!`, `a.x_usd as unknown as number`). */
const TS_MONEY_OPERAND = `:matches(${["", "expression.", "expression.expression."]
  .flatMap((wrapper) => [
    `[${wrapper}name=${MONEY_NAME}]`,
    `[${wrapper}property.name=${MONEY_NAME}]`,
    `[${wrapper}property.value=${MONEY_NAME}]`,
  ])
  .join(", ")})`;

// esquery regexes cannot contain `/`, so division is spelled `\x2F`.
const ARITHMETIC_OPERATOR = String.raw`/^([-+*%]|\x2F|\*\*)$/`;
const COMPOUND_ASSIGNMENT = String.raw`/^([-+*%&|^]|\x2F|\*\*|<<|>>>?)=$/`;

const TS_MONEY_ARITHMETIC = [
  `BinaryExpression[operator=${ARITHMETIC_OPERATOR}] > ${TS_MONEY_OPERAND}`,
  `UnaryExpression[operator=/^[-+~]$/] > ${TS_MONEY_OPERAND}`,
  `UpdateExpression > ${TS_MONEY_OPERAND}`,
  `AssignmentExpression[operator=${COMPOUND_ASSIGNMENT}] > ${TS_MONEY_OPERAND}`,
  `CallExpression[callee.name=/^(Number|parseFloat|parseInt|BigInt)$/] > ${TS_MONEY_OPERAND}`,
  `CallExpression[callee.object.name=/^(Number|Math)$/] > ${TS_MONEY_OPERAND}`,
].map((selector) => ({ selector, message: MONEY_MESSAGE }));

/** The same operand in an Angular template expression, at `path`: a property
 * or safe read (`name`), a keyed read (`key.value`), and either of those
 * behind `!` or parentheses (`expression.…`). */
const templateMoneyOperand = (path) =>
  [
    `[${path}.name=${MONEY_NAME}]`,
    `[${path}.key.value=${MONEY_NAME}]`,
    `[${path}.expression.name=${MONEY_NAME}]`,
    `[${path}.expression.key.value=${MONEY_NAME}]`,
  ].join(", ");

const TEMPLATE_ARITHMETIC_OPERATOR = String.raw`/^([-+*%]|\x2F|\*\*)=?$/`;

const TEMPLATE_MONEY_ARITHMETIC = [
  `Binary[operation=${TEMPLATE_ARITHMETIC_OPERATOR}]:matches(${templateMoneyOperand("left")}, ${templateMoneyOperand("right")})`,
  `Unary:matches(${templateMoneyOperand("expr")})`,
].map((selector) => ({ selector, message: MONEY_MESSAGE }));

const COERCION_MESSAGE =
  "PRD #2560 D12: a money surface never turns a value into a number. Render the Python-authored string; ask the backend for any figure it lacks.";

const MONEY_SURFACE_COERCION = [
  { selector: "UnaryExpression[operator=/^[-+]$/][argument.type!='Literal']", message: COERCION_MESSAGE },
  {
    selector: "MemberExpression[object.name=/^(window|globalThis|self)$/][property.name=/^(Number|parseFloat|parseInt|BigInt|Math)$/]",
    message: COERCION_MESSAGE,
  },
];

const TEMPLATE_MONEY_SURFACE_ARITHMETIC = [
  { selector: `Binary[operation=${TEMPLATE_ARITHMETIC_OPERATOR}]`, message: COERCION_MESSAGE },
  { selector: "Unary:not([expr.value])", message: COERCION_MESSAGE },
  // Angular's numeric pipes parse before they format: on a money surface
  // nothing is ever handed to one. Authored strings render through
  // `authoredUsd` (pure string formatting) or as given.
  {
    selector: "BindingPipe[name=/^(currency|number|percent)$/]",
    message:
      "PRD #2560 D12: a money surface never hands a value to a pipe that formats numbers — it parses before it formats. Render the Python-authored string with `authoredUsd` or as given.",
  },
];

const moneySurfaces = (extension) => [
  `src/app/**/*money*.${extension}`,
  `src/app/**/*money*/**/*.${extension}`,
  `src/app/**/*budget*.${extension}`,
  `src/app/**/*budget*/**/*.${extension}`,
  `src/app/components/brokers/alpaca-workspace/**/*.${extension}`,
  // Home's money: its page, bot rows, Wall tiles and Finished results (its
  // bar is `home-money`, a money surface by name).
  ...["alpaca-home", "home-bot-row", "home-bot-tile", "home-finished"].map(
    (name) => `src/app/components/brokers/alpaca-home/${name}.component.${extension}`,
  ),
  `src/app/components/brokers/alpaca-desk/alpaca-account-list-page.component.${extension}`,
  `src/app/components/brokers/alpaca-desk/alpaca-lane-card.component.${extension}`,
];

export default tseslint.config(
  {
    ignores: [
      "dist/",
      "node_modules/",
      ".angular/",
      // Auto-generated from the Python service's OpenAPI spec — do not
      // hand-edit. Regenerate with ``npx openapi-typescript`` (see
      // src/app/api/broker-models.ts).
      "src/app/api/broker.types.ts",
    ],
  },
  {
    files: ["**/*.ts"],
    extends: [
      eslint.configs.recommended,
      ...tseslint.configs.strict,
      ...tseslint.configs.stylistic,
      ...angular.configs.tsRecommended,
    ],
    plugins: {
      "unused-imports": unusedImports,
    },
    processor: angular.processInlineTemplates,
    rules: {
      "@angular-eslint/directive-selector": [
        "error",
        { type: "attribute", prefix: "app", style: "camelCase" },
      ],
      "@angular-eslint/component-selector": [
        "error",
        { type: "element", prefix: "app", style: "kebab-case" },
      ],
      "@typescript-eslint/no-empty-function": "off",
      "@typescript-eslint/no-explicit-any": "warn",
      "@typescript-eslint/no-non-null-assertion": "warn",
      // Use unused-imports plugin for auto-fixable unused import removal
      "@typescript-eslint/no-unused-vars": "off",
      "unused-imports/no-unused-imports": "error",
      "unused-imports/no-unused-vars": [
        "warn",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      "@typescript-eslint/no-extraneous-class": "off",
    },
  },
  {
    files: ["**/*.ts"],
    ignores: ["src/app/shared/charts/chart-utils.ts"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          paths: [
            {
              name: "lightweight-charts",
              importNames: ["createChart"],
              message:
                "Use createAppChart from shared/charts/chart-utils so application-wide chart policy is applied.",
            },
          ],
        },
      ],
    },
  },
  {
    files: ["**/*.html"],
    extends: [
      ...angular.configs.templateRecommended,
      ...angular.configs.templateAccessibility,
    ],
    rules: {
      // Accessibility — downgraded to warn for this trading dashboard
      "@angular-eslint/template/label-has-associated-control": "warn",
      "@angular-eslint/template/click-events-have-key-events": "warn",
      "@angular-eslint/template/interactive-supports-focus": "warn",
    },
  },
  // No money arithmetic, layer 1: everywhere (see the note at the top).
  {
    files: ["src/app/**/*.ts"],
    ignores: ["**/*.spec.ts"],
    rules: { "no-restricted-syntax": ["error", ...TS_MONEY_ARITHMETIC] },
  },
  {
    files: ["src/app/**/*.html"],
    rules: { "no-restricted-syntax": ["error", ...TEMPLATE_MONEY_ARITHMETIC] },
  },
  // Layer 2: money surfaces. `no-restricted-syntax` options replace rather
  // than merge, so each block restates layer 1's selectors.
  {
    files: moneySurfaces("ts"),
    ignores: ["**/*.spec.ts"],
    plugins: { "learn-ai": { rules: { "no-money-alias-arithmetic": noMoneyAliasArithmetic } } },
    rules: {
      "no-implicit-coercion": ["error", { boolean: false }],
      "no-restricted-globals": [
        "error",
        ...["Number", "parseFloat", "parseInt", "BigInt", "Math"].map((name) => ({ name, message: COERCION_MESSAGE })),
      ],
      "no-restricted-syntax": ["error", ...TS_MONEY_ARITHMETIC, ...MONEY_SURFACE_COERCION],
      "learn-ai/no-money-alias-arithmetic": "error",
    },
  },
  {
    files: moneySurfaces("html"),
    rules: {
      "no-restricted-syntax": ["error", ...TEMPLATE_MONEY_ARITHMETIC, ...TEMPLATE_MONEY_SURFACE_ARITHMETIC],
    },
  }
);
