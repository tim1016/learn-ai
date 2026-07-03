const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const ts = require("typescript");

const frontendRoot = path.join(__dirname, "..");
const typeFilePath = path.join(
  frontendRoot,
  "src",
  "app",
  "api",
  "live-instances.types.ts",
);
const fixtureDir = path.join(
  frontendRoot,
  "src",
  "testing",
  "operator_surface_fixtures",
);
const fixturePaths = [
  ["steady", path.join(fixtureDir, "steady.json")],
  ["stopped", path.join(fixtureDir, "stopped.json")],
];

const primitiveFlags =
  ts.TypeFlags.String |
  ts.TypeFlags.Number |
  ts.TypeFlags.Boolean |
  ts.TypeFlags.BigInt |
  ts.TypeFlags.ESSymbol |
  ts.TypeFlags.StringLiteral |
  ts.TypeFlags.NumberLiteral |
  ts.TypeFlags.BooleanLiteral |
  ts.TypeFlags.Null |
  ts.TypeFlags.Undefined |
  ts.TypeFlags.Void |
  ts.TypeFlags.Any |
  ts.TypeFlags.Unknown |
  ts.TypeFlags.Never;

const program = createProgram();
const checker = program.getTypeChecker();
const typeSourceFile = program.getSourceFile(typeFilePath);
if (!typeSourceFile) {
  throw new Error(`Could not load ${typeFilePath}`);
}

const rootType = getExportedType(typeSourceFile, "LiveInstanceStatus");
const failures = [];
const checkedLiteralPaths = new Set();
let checkedLiteralCount = 0;

for (const [fixtureName, fixturePath] of fixturePaths) {
  const fixture = JSON.parse(fs.readFileSync(fixturePath, "utf8"));
  failures.push(...validateValue(rootType, fixture, fixtureName));
}

if (failures.length > 0) {
  console.error("LiveInstanceStatus fixture literal contract failed:");
  for (const failure of failures) {
    console.error(`- ${failure}`);
  }
  process.exit(1);
}

assertCoverage();

console.log(
  `live instance literal contract guard ok (${checkedLiteralCount} closed literal values checked)`,
);

function createProgram() {
  const configPath = ts.findConfigFile(
    frontendRoot,
    ts.sys.fileExists,
    "tsconfig.json",
  );
  if (!configPath) {
    throw new Error(`Could not find tsconfig.json under ${frontendRoot}`);
  }
  const config = ts.readConfigFile(configPath, ts.sys.readFile);
  if (config.error) {
    throw new Error(
      ts.flattenDiagnosticMessageText(config.error.messageText, "\n"),
    );
  }
  const parsed = ts.parseJsonConfigFileContent(
    config.config,
    ts.sys,
    path.dirname(configPath),
    { noEmit: true },
    configPath,
  );
  const diagnostics = parsed.errors.map((diagnostic) =>
    ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n"),
  );
  if (diagnostics.length > 0) {
    throw new Error(
      `Could not parse tsconfig.json:\n${diagnostics.join("\n")}`,
    );
  }
  return ts.createProgram({
    rootNames: [typeFilePath],
    options: parsed.options,
  });
}

function getExportedType(file, exportName) {
  const moduleSymbol = checker.getSymbolAtLocation(file);
  if (!moduleSymbol) {
    throw new Error(`Could not inspect exports for ${file.fileName}`);
  }
  const symbol = checker
    .getExportsOfModule(moduleSymbol)
    .find((candidate) => candidate.getName() === exportName);
  if (!symbol) {
    throw new Error(`${exportName} is not exported from ${file.fileName}`);
  }
  return checker.getDeclaredTypeOfSymbol(symbol);
}

function validateValue(type, value, valuePath) {
  const nullable = allowsNullish(type);
  if (value === null || value === undefined) {
    if (nullable) return [];
    if (hasClosedLiteral(type)) {
      return [
        `${valuePath} is ${String(value)} but ${checker.typeToString(type)} is required`,
      ];
    }
    return [];
  }

  const literalValues = stringLiteralValues(type);
  if (literalValues) {
    checkedLiteralCount += 1;
    checkedLiteralPaths.add(valuePath);
    if (typeof value !== "string") {
      return [
        `${valuePath} is ${typeof value}; expected ${literalValues.join(" | ")}`,
      ];
    }
    if (!literalValues.includes(value)) {
      return [
        `${valuePath}=${JSON.stringify(value)} is not one of ${literalValues.join(" | ")}`,
      ];
    }
    return [];
  }

  const variants = nonNullishTypes(type);
  if (variants.length > 1) {
    if (!hasClosedLiteral(type)) return [];
    const branchResults = variants.map((variant) =>
      validateValue(variant, value, valuePath),
    );
    if (branchResults.some((result) => result.length === 0)) return [];
    const shortest = branchResults.reduce((best, result) =>
      result.length < best.length ? result : best,
    );
    return shortest.length > 0
      ? shortest
      : [`${valuePath} did not match ${checker.typeToString(type)}`];
  }

  const [nonNullishType] = variants;
  const elementType = arrayElementType(nonNullishType);
  if (elementType) {
    if (!Array.isArray(value)) {
      return hasClosedLiteral(elementType)
        ? [`${valuePath} is not an array`]
        : [];
    }
    return value.flatMap((item, index) =>
      validateValue(elementType, item, `${valuePath}[${index}]`),
    );
  }

  if (!isObjectLikeValue(value) || isPrimitiveType(nonNullishType)) {
    return [];
  }

  const errors = [];
  const properties = checker.getPropertiesOfType(nonNullishType);
  for (const property of properties) {
    const propertyName = property.getName();
    if (propertyName.startsWith("__@")) continue;
    const propertyType = checker.getTypeOfSymbolAtLocation(
      property,
      property.valueDeclaration ?? typeSourceFile,
    );
    if (!hasClosedLiteral(propertyType)) continue;
    const nextPath = childPath(valuePath, propertyName);
    if (!Object.prototype.hasOwnProperty.call(value, propertyName)) {
      if (!isOptionalProperty(property) && !allowsNullish(propertyType)) {
        errors.push(
          `${nextPath} is missing but ${checker.typeToString(propertyType)} is required`,
        );
      }
      continue;
    }
    errors.push(...validateValue(propertyType, value[propertyName], nextPath));
  }

  const indexType = checker.getIndexTypeOfType(
    nonNullishType,
    ts.IndexKind.String,
  );
  if (indexType && hasClosedLiteral(indexType)) {
    const declared = new Set(properties.map((property) => property.getName()));
    for (const [key, item] of Object.entries(value)) {
      if (declared.has(key)) continue;
      errors.push(...validateValue(indexType, item, childPath(valuePath, key)));
    }
  }

  return errors;
}

function hasClosedLiteral(type, seen = new Set()) {
  const key = type.id ?? checker.typeToString(type);
  if (seen.has(key)) return false;
  seen.add(key);

  if (stringLiteralValues(type)) return true;

  const variants = nonNullishTypes(type);
  if (variants.length > 1) {
    return variants.some((variant) => hasClosedLiteral(variant, seen));
  }

  const [nonNullishType] = variants;
  if (!nonNullishType || isPrimitiveType(nonNullishType)) return false;

  const elementType = arrayElementType(nonNullishType);
  if (elementType) return hasClosedLiteral(elementType, seen);

  for (const property of checker.getPropertiesOfType(nonNullishType)) {
    const propertyType = checker.getTypeOfSymbolAtLocation(
      property,
      property.valueDeclaration ?? typeSourceFile,
    );
    if (hasClosedLiteral(propertyType, seen)) return true;
  }

  const indexType = checker.getIndexTypeOfType(
    nonNullishType,
    ts.IndexKind.String,
  );
  return indexType ? hasClosedLiteral(indexType, seen) : false;
}

function stringLiteralValues(type) {
  const variants = nonNullishTypes(type);
  if (variants.length === 0) return null;
  const values = [];
  for (const variant of variants) {
    if ((variant.flags & ts.TypeFlags.StringLiteral) === 0) return null;
    values.push(variant.value);
  }
  return values;
}

function nonNullishTypes(type) {
  const variants = type.isUnion() ? type.types : [type];
  return variants.filter((variant) => !isNullishType(variant));
}

function allowsNullish(type) {
  const variants = type.isUnion() ? type.types : [type];
  return variants.some(isNullishType);
}

function isNullishType(type) {
  return (
    (type.flags & ts.TypeFlags.Null) !== 0 ||
    (type.flags & ts.TypeFlags.Undefined) !== 0 ||
    (type.flags & ts.TypeFlags.Void) !== 0
  );
}

function isPrimitiveType(type) {
  return (type.flags & primitiveFlags) !== 0;
}

function arrayElementType(type) {
  if (!checker.isArrayType(type) && !checker.isTupleType(type)) return null;
  return checker.getTypeArguments(type)[0] ?? null;
}

function isObjectLikeValue(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isOptionalProperty(symbol) {
  return (symbol.flags & ts.SymbolFlags.Optional) !== 0;
}

function childPath(parent, key) {
  return /^[A-Za-z_$][\w$]*$/.test(key)
    ? `${parent}.${key}`
    : `${parent}[${JSON.stringify(key)}]`;
}

function assertCoverage() {
  const requiredCoverage = [
    [
      "prior run classification",
      (item) => item.endsWith(".operator_surface.prior_run.classification"),
    ],
    [
      "trader guidance proof-line tone",
      (item) =>
        item.includes(".operator_surface.trader_guidance.proof_lines[") &&
        item.endsWith(".tone"),
    ],
    [
      "invoke-endpoint remediation endpoint",
      (item) =>
        item.includes(
          ".operator_surface.trader_guidance.additional_attention_groups[",
        ) && item.endsWith(".remediation.endpoint"),
    ],
    [
      "invoke-endpoint remediation method",
      (item) =>
        item.includes(
          ".operator_surface.trader_guidance.additional_attention_groups[",
        ) && item.endsWith(".remediation.method"),
    ],
    [
      "invoke-endpoint remediation path template",
      (item) =>
        item.includes(
          ".operator_surface.trader_guidance.additional_attention_groups[",
        ) && item.endsWith(".remediation.path_template"),
    ],
    [
      "lifecycle chart node status",
      (item) =>
        item.includes(".lifecycle_chart.global_graph.nodes[") &&
        item.endsWith(".status"),
    ],
    [
      "lifecycle subgraph record values",
      (item) =>
        item.includes(".lifecycle_chart.subgraphs.") &&
        item.includes(".nodes[") &&
        item.endsWith(".status"),
    ],
  ];

  for (const [label, predicate] of requiredCoverage) {
    assert(
      [...checkedLiteralPaths].some(predicate),
      `LiveInstanceStatus literal guard did not exercise ${label}`,
    );
  }
}
