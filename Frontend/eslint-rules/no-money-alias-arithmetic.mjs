// A scope-aware companion to the name-based money selectors in
// `eslint.config.mjs` (PRD #2560 D12). A plain-name alias hides a money value
// from those selectors — `const width = segment.share_bps; width / 100` — so
// on money surfaces this rule resolves each operand identifier to its
// declaration and refuses arithmetic (or numeric coercion) on one whose value
// came, directly or through further aliases, from a `*_usd` / `*_bps` field.
//
// Proven to fire by `scripts/verify-money-arithmetic-lint-guard.cjs`.

const MONEY_NAME = /_(usd|bps)$/;
const ARITHMETIC_OPERATOR = /^([-+*%&|^]|\/|\*\*|<<|>>>?)$/;
const COMPOUND_ASSIGNMENT = /^([-+*%&|^]|\/|\*\*|<<|>>>?)=$/;
const COERCION_CALL = /^(Number|parseFloat|parseInt|BigInt)$/;

const unwrapType = (node) => {
  while (node && ["TSNonNullExpression", "TSAsExpression", "TSSatisfiesExpression", "TSTypeAssertion"].includes(node.type)) {
    node = node.expression;
  }
  return node;
};

const isMoneyNamed = (node) =>
  (node.type === "Identifier" && MONEY_NAME.test(node.name)) ||
  (node.type === "Literal" && typeof node.value === "string" && MONEY_NAME.test(node.value));

/** Whether the expression's value is a money field: `x_usd`, `a.x_usd`,
 * `a?.x_usd`, `a["x_usd"]`, with or without TypeScript wrappers. */
const isMoneyValue = (node) => {
  const value = unwrapType(node);
  if (!value) return false;
  if (value.type === "Identifier") return MONEY_NAME.test(value.name);
  return value.type === "MemberExpression" && isMoneyNamed(value.property);
};

/** Whether `declarator` gives `identifier` a value that came from a money
 * field — a direct alias (`const f = view.free_usd`) or a destructured rename
 * (`const { share_bps: width } = segment`, plain or renamed). */
const declaresMoneyAlias = (declarator, identifier, resolve, depth = 0) => {
  if (depth > 4) return false;
  if (declarator.id.type === "Identifier" && declarator.id.name === identifier.name) {
    const init = unwrapType(declarator.init);
    if (!init) return false;
    return isMoneyValue(init) || (init.type === "Identifier" && resolve(init, depth + 1));
  }
  if (declarator.id.type === "ObjectPattern") {
    return declarator.id.properties.some(
      (property) =>
        property.type === "Property" &&
        isMoneyNamed(property.key) &&
        property.value.type === "Identifier" &&
        property.value.name === identifier.name,
    );
  }
  return false;
};

/** Resolve an identifier to the variable it names, through enclosing scopes. */
const findVariable = (sourceCode, identifier) => {
  let scope = sourceCode.getScope(identifier);
  while (scope) {
    const variable = scope.set.get(identifier.name);
    if (variable) return variable;
    scope = scope.upper;
  }
  return undefined;
};

export default {
  meta: {
    type: "problem",
    schema: [],
    messages: {
      moneyAlias:
        "PRD #2560 D12: an alias of a money value is still Python-authored. Render the *_usd string or *_bps width as given; ask the backend for any figure it lacks.",
    },
  },
  create(context) {
    const { sourceCode } = context;

    const resolvesToMoney = (identifier, depth = 0) => {
      const variable = findVariable(sourceCode, identifier);
      return (
        variable?.defs.some(
          (definition) =>
            definition.type === "Variable" && declaresMoneyAlias(definition.node, identifier, resolvesToMoney, depth),
        ) ?? false
      );
    };

    const check = (node, operands) => {
      for (const operand of operands) {
        if (operand.type === "Identifier" && resolvesToMoney(operand)) {
          context.report({ node, messageId: "moneyAlias" });
          return;
        }
      }
    };

    return {
      BinaryExpression: (node) => {
        if (ARITHMETIC_OPERATOR.test(node.operator)) check(node, [node.left, node.right]);
      },
      UnaryExpression: (node) => {
        if (/^[-+~]$/.test(node.operator)) check(node, [node.argument]);
      },
      UpdateExpression: (node) => check(node, [node.argument]),
      AssignmentExpression: (node) => {
        if (COMPOUND_ASSIGNMENT.test(node.operator)) check(node, [node.left, node.right]);
      },
      CallExpression: (node) => {
        const callee = node.callee;
        const isCoercion =
          (callee.type === "Identifier" && COERCION_CALL.test(callee.name)) ||
          (callee.type === "MemberExpression" &&
            !callee.computed &&
            callee.object.type === "Identifier" &&
            /^(Math|Number)$/.test(callee.object.name));
        if (isCoercion) check(node, node.arguments);
      },
    };
  },
};
