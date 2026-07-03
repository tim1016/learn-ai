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

const rootType = getExportedType(typeSourceFile, "OperatorSurface");
const failures = [];
const checkedLiteralPaths = new Set();
let checkedLiteralCount = 0;
const loadedFixtures = [];

for (const [fixtureName, fixturePath] of fixturePaths) {
  const fixture = JSON.parse(fs.readFileSync(fixturePath, "utf8"));
  loadedFixtures.push([fixtureName, fixture]);
  failures.push(...validateValue(rootType, fixture, fixtureName));
}

if (failures.length > 0) {
  console.error("OperatorSurface fixture literal contract failed:");
  for (const failure of failures) {
    console.error(`- ${failure}`);
  }
  process.exit(1);
}

assertCoverage();
assertNestedExtraKeyProbe();
assertPrimitiveIndexValueProbe();

console.log(
  `operator surface literal contract guard ok (${checkedLiteralCount} closed literal values checked)`,
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
  const primitiveErrors = validatePrimitiveKind(type, value, valuePath);
  if (primitiveErrors) return primitiveErrors;

  if (variants.length > 1) {
    if (!shouldValidateType(type)) return [];
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
      return shouldValidateType(elementType)
        ? [`${valuePath} is ${valueKind(value)}; expected array`]
        : [];
    }
    if (!shouldValidateType(elementType)) return [];
    return value.flatMap((item, index) =>
      validateValue(elementType, item, `${valuePath}[${index}]`),
    );
  }

  if (!isObjectLikeValue(value)) {
    return shouldValidateType(nonNullishType)
      ? [
          `${valuePath} is ${valueKind(value)}; expected ${checker.typeToString(nonNullishType)}`,
        ]
      : [];
  }

  if (isPrimitiveType(nonNullishType)) {
    return [];
  }

  const errors = [];
  const properties = publicPropertiesOfType(nonNullishType);
  const declared = new Set(properties.map((property) => property.getName()));
  const indexType = checker.getIndexTypeOfType(
    nonNullishType,
    ts.IndexKind.String,
  );

  for (const [key, item] of Object.entries(value)) {
    if (declared.has(key)) continue;
    const nextPath = childPath(valuePath, key);
    if (!indexType) {
      errors.push(
        `${nextPath} is not declared on ${checker.typeToString(nonNullishType)}`,
      );
      continue;
    }
    if (shouldValidateType(indexType)) {
      errors.push(...validateValue(indexType, item, nextPath));
    }
  }

  for (const property of properties) {
    const propertyName = property.getName();
    const propertyType = checker.getTypeOfSymbolAtLocation(
      property,
      property.valueDeclaration ?? typeSourceFile,
    );
    if (!shouldValidateType(propertyType)) continue;
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

  return errors;
}

function shouldValidateType(type, seen = new Set()) {
  const key = type.id ?? checker.typeToString(type);
  if (seen.has(key)) return false;
  seen.add(key);

  if (primitiveKindsForType(type) !== null) return true;
  if (hasInspectableShape(type)) return true;

  const variants = nonNullishTypes(type);
  if (variants.length > 1) {
    return variants.some((variant) => shouldValidateType(variant, seen));
  }

  const [nonNullishType] = variants;
  if (!nonNullishType || isPrimitiveType(nonNullishType)) return false;

  const elementType = arrayElementType(nonNullishType);
  if (elementType) return shouldValidateType(elementType, seen);

  const indexType = checker.getIndexTypeOfType(
    nonNullishType,
    ts.IndexKind.String,
  );
  return indexType ? shouldValidateType(indexType, seen) : false;
}

function hasInspectableShape(type, seen = new Set()) {
  const key = type.id ?? checker.typeToString(type);
  if (seen.has(key)) return false;
  seen.add(key);

  if (hasClosedLiteral(type)) return true;

  const variants = nonNullishTypes(type);
  if (variants.length > 1) {
    return variants.some((variant) => hasInspectableShape(variant, seen));
  }

  const [nonNullishType] = variants;
  if (!nonNullishType || isPrimitiveType(nonNullishType)) return false;

  const elementType = arrayElementType(nonNullishType);
  if (elementType) return hasInspectableShape(elementType, seen);

  if (publicPropertiesOfType(nonNullishType).length > 0) return true;

  const indexType = checker.getIndexTypeOfType(
    nonNullishType,
    ts.IndexKind.String,
  );
  return indexType ? hasInspectableShape(indexType, seen) : false;
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

  for (const property of publicPropertiesOfType(nonNullishType)) {
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

function validatePrimitiveKind(type, value, valuePath) {
  const kinds = primitiveKindsForType(type);
  if (!kinds) return null;
  const actual = valueKind(value);
  if (kinds.has(actual)) return [];
  return [`${valuePath} is ${actual}; expected ${[...kinds].join(" | ")}`];
}

function primitiveKindsForType(type) {
  const variants = nonNullishTypes(type);
  if (variants.length === 0) return null;

  const kinds = new Set();
  for (const variant of variants) {
    const kind = primitiveKindForType(variant);
    if (!kind) return null;
    kinds.add(kind);
  }
  return kinds.size > 0 ? kinds : null;
}

function primitiveKindForType(type) {
  if ((type.flags & ts.TypeFlags.StringLike) !== 0) return "string";
  if ((type.flags & ts.TypeFlags.NumberLike) !== 0) return "number";
  if ((type.flags & ts.TypeFlags.BooleanLike) !== 0) return "boolean";
  if ((type.flags & ts.TypeFlags.BigIntLike) !== 0) return "bigint";
  if ((type.flags & ts.TypeFlags.ESSymbolLike) !== 0) return "symbol";
  return null;
}

function arrayElementType(type) {
  if (!checker.isArrayType(type) && !checker.isTupleType(type)) return null;
  return checker.getTypeArguments(type)[0] ?? null;
}

function isObjectLikeValue(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function valueKind(value) {
  if (Array.isArray(value)) return "array";
  if (value === null) return "null";
  return typeof value;
}

function publicPropertiesOfType(type) {
  return checker
    .getPropertiesOfType(type)
    .filter((property) => !property.getName().startsWith("__@"));
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
      (item) => item.endsWith(".prior_run.classification"),
    ],
    [
      "trader guidance proof-line tone",
      (item) =>
        item.includes(".trader_guidance.proof_lines[") &&
        item.endsWith(".tone"),
    ],
    [
      "invoke-endpoint remediation endpoint",
      (item) =>
        item.includes(
          ".trader_guidance.additional_attention_groups[",
        ) && item.endsWith(".remediation.endpoint"),
    ],
    [
      "invoke-endpoint remediation method",
      (item) =>
        item.includes(
          ".trader_guidance.additional_attention_groups[",
        ) && item.endsWith(".remediation.method"),
    ],
    [
      "invoke-endpoint remediation path template",
      (item) =>
        item.includes(
          ".trader_guidance.additional_attention_groups[",
        ) && item.endsWith(".remediation.path_template"),
    ],
  ];

  for (const [label, predicate] of requiredCoverage) {
    assert(
      [...checkedLiteralPaths].some(predicate),
      `OperatorSurface literal guard did not exercise ${label}`,
    );
  }
}

function assertNestedExtraKeyProbe() {
  const [fixtureName, fixture] = loadedFixtures[0] ?? [];
  assert(
    fixtureName && fixture,
    "operator surface literal guard needs a probe fixture",
  );

  const probe = JSON.parse(JSON.stringify(fixture));
  assert(
    isObjectLikeValue(probe.host_process),
    "operator surface literal guard probe needs a host_process object",
  );
  probe.host_process.__unexpected_nested_contract_key__ = true;

  const probeFailures = withPreservedLiteralCoverage(() =>
    validateValue(rootType, probe, `${fixtureName}_extra_key_probe`),
  );

  assert(
    probeFailures.some(
      (failure) =>
        failure.includes(
          `${fixtureName}_extra_key_probe.host_process.__unexpected_nested_contract_key__`,
        ) && failure.includes("is not declared"),
    ),
    "OperatorSurface literal guard did not reject a nested extra key",
  );
}

function assertPrimitiveIndexValueProbe() {
  const [fixtureName, fixture] =
    loadedFixtures.find(
      ([, candidate]) =>
        isObjectLikeValue(candidate.runtime_freshness) &&
        isObjectLikeValue(candidate.runtime_freshness.headline) &&
        isObjectLikeValue(
          candidate.runtime_freshness.headline.forensic_facts,
        ),
    ) ?? [];
  assert(
    fixtureName && fixture,
    "operator surface literal guard needs a forensic_facts probe fixture",
  );

  const probe = JSON.parse(JSON.stringify(fixture));
  probe.runtime_freshness.headline.forensic_facts.__unexpected_object_value__ = {
    nested: true,
  };
  const probeFailures = withPreservedLiteralCoverage(() =>
    validateValue(rootType, probe, `${fixtureName}_primitive_index_probe`),
  );

  assert(
    probeFailures.some(
      (failure) =>
        failure.includes(
          `${fixtureName}_primitive_index_probe.runtime_freshness.headline.forensic_facts.__unexpected_object_value__`,
        ) && failure.includes("expected string | number | boolean"),
    ),
    "OperatorSurface literal guard did not reject an object value in a primitive-indexed record",
  );

  const recordProbe = JSON.parse(JSON.stringify(fixture));
  recordProbe.runtime_freshness.headline.forensic_facts = "not-a-record";
  const recordProbeFailures = withPreservedLiteralCoverage(() =>
    validateValue(
      rootType,
      recordProbe,
      `${fixtureName}_primitive_index_record_probe`,
    ),
  );

  assert(
    recordProbeFailures.some(
      (failure) =>
        failure.includes(
          `${fixtureName}_primitive_index_record_probe.runtime_freshness.headline.forensic_facts`,
        ) && failure.includes("expected Record"),
    ),
    "OperatorSurface literal guard did not reject a primitive value replacing a primitive-indexed record",
  );
}

function withPreservedLiteralCoverage(action) {
  const priorCount = checkedLiteralCount;
  const priorPaths = new Set(checkedLiteralPaths);
  try {
    return action();
  } finally {
    checkedLiteralCount = priorCount;
    checkedLiteralPaths.clear();
    for (const item of priorPaths) {
      checkedLiteralPaths.add(item);
    }
  }
}
