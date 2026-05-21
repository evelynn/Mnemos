#!/usr/bin/env node
/**
 * Mnemos TypeScript analyzer (Phase 1).
 *
 * Implements the CLI contract in docs/analyzer-contract.md: probe, inventory,
 * symbols, calls, contracts, data_access, schema.
 *
 * Follows spec §7.2 — uses the official TypeScript Compiler API so both JS and
 * TS files can be analysed with minimal project configuration.
 */

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import ts from "typescript";

const SOURCE_NAME = "ggoss-ts";
const SOURCE_VERSION = "1.0.0";

function envelope(recordType, data) {
  return JSON.stringify({
    record_type: recordType,
    source_name: SOURCE_NAME,
    source_version: SOURCE_VERSION,
    analyzed_at: new Date().toISOString(),
    data,
  });
}

function writeLine(outStream, line) {
  outStream.write(line + "\n");
}

function reportError(file, message, recoverable = true) {
  process.stderr.write(
    JSON.stringify({ level: "error", file, message, recoverable }) + "\n",
  );
}

function parseCommon(argv) {
  let outPath = null;
  let target = null;
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--output" && i + 1 < argv.length) {
      outPath = argv[++i];
    } else if (target === null) {
      target = argv[i];
    }
  }
  return { target, outPath };
}

function openOutput(outPath) {
  if (!outPath) return process.stdout;
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  return fs.createWriteStream(outPath, { encoding: "utf-8" });
}

function walkFiles(dir, exts, collected = []) {
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch (err) {
    reportError(dir, err.message);
    return collected;
  }
  for (const e of entries) {
    if (e.name === "node_modules" || e.name.startsWith(".")) continue;
    const full = path.join(dir, e.name);
    if (e.isDirectory()) walkFiles(full, exts, collected);
    else if (exts.some((ext) => e.name.endsWith(ext))) collected.push(full);
  }
  return collected;
}

function cmdProbe(target) {
  if (!target || !fs.existsSync(target)) {
    return { applicable: false, reason: "path_not_found", files_found: 0 };
  }
  const files = walkFiles(target, [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"]);
  const hasTsconfig = fs.existsSync(path.join(target, "tsconfig.json"));
  const hasPkg = fs.existsSync(path.join(target, "package.json"));
  const applicable = files.length > 0 || hasTsconfig || hasPkg;
  return {
    applicable,
    reason: applicable
      ? `found ${files.length} ts/js files; tsconfig=${hasTsconfig} pkg=${hasPkg}`
      : "no_js_or_ts_sources",
    files_found: files.length,
  };
}

function cmdInventory(target) {
  const files = walkFiles(target, [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"]);
  return {
    files: files.slice(0, 5000).map((f) => path.relative(target, f)),
    modules: fs.existsSync(path.join(target, "package.json"))
      ? [path.relative(target, path.join(target, "package.json"))]
      : [],
    tsconfigs: fs.existsSync(path.join(target, "tsconfig.json"))
      ? [path.relative(target, path.join(target, "tsconfig.json"))]
      : [],
    errors: [],
  };
}

function componentId(target) {
  try {
    const pkg = JSON.parse(
      fs.readFileSync(path.join(target, "package.json"), "utf-8"),
    );
    return `svc.${pkg.name ?? path.basename(target)}`;
  } catch {
    return `svc.${path.basename(target)}`;
  }
}

function buildProgram(target) {
  const tsconfigPath = path.join(target, "tsconfig.json");
  if (fs.existsSync(tsconfigPath)) {
    const raw = ts.readConfigFile(tsconfigPath, ts.sys.readFile);
    const parsed = ts.parseJsonConfigFileContent(
      raw.config || {},
      ts.sys,
      target,
    );
    return ts.createProgram({
      rootNames: parsed.fileNames,
      options: parsed.options,
    });
  }
  const files = walkFiles(target, [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"]);
  return ts.createProgram({
    rootNames: files,
    options: {
      allowJs: true,
      checkJs: false,
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.ESNext,
      moduleResolution: ts.ModuleResolutionKind.NodeNext,
      jsx: ts.JsxEmit.Preserve,
      noEmit: true,
      skipLibCheck: true,
    },
  });
}

function symbolIdFor(sf, node, name) {
  const { line, character } = sf.getLineAndCharacterOfPosition(node.getStart(sf));
  return `ts:${path.basename(sf.fileName)}:${name}@${line + 1}:${character + 1}`;
}

function visibilityOf(node) {
  if (node.modifiers?.some((m) => m.kind === ts.SyntaxKind.PrivateKeyword))
    return "private";
  if (node.modifiers?.some((m) => m.kind === ts.SyntaxKind.ProtectedKeyword))
    return "internal";
  return "public";
}

function emitSymbol(out, sf, node, kind, name, compId) {
  const start = sf.getLineAndCharacterOfPosition(node.getStart(sf));
  const id = symbolIdFor(sf, node, name);
  const data = {
    id,
    kind,
    name,
    component_id: compId,
    signature: node.getText(sf).slice(0, 400),
    location: {
      file: sf.fileName,
      line: start.line + 1,
      col: start.character + 1,
    },
    visibility: visibilityOf(node),
    is_entry_point: false,
    xml_doc: null,
    metadata: {},
    certainty: "asserted",
    created_by: [SOURCE_NAME],
  };
  writeLine(out, envelope("symbol", data));
}

function cmdSymbols(target, outPath) {
  const program = buildProgram(target);
  const out = openOutput(outPath);
  const compId = componentId(target);

  for (const sf of program.getSourceFiles()) {
    if (sf.isDeclarationFile) continue;
    if (sf.fileName.includes("node_modules")) continue;

    const visit = (node) => {
      switch (node.kind) {
        case ts.SyntaxKind.ClassDeclaration: {
          const n = node;
          if (n.name) emitSymbol(out, sf, n, "class", n.name.text, compId);
          break;
        }
        case ts.SyntaxKind.InterfaceDeclaration: {
          const n = node;
          emitSymbol(out, sf, n, "interface", n.name.text, compId);
          break;
        }
        case ts.SyntaxKind.TypeAliasDeclaration: {
          const n = node;
          emitSymbol(out, sf, n, "type", n.name.text, compId);
          break;
        }
        case ts.SyntaxKind.EnumDeclaration: {
          const n = node;
          emitSymbol(out, sf, n, "enum", n.name.text, compId);
          break;
        }
        case ts.SyntaxKind.FunctionDeclaration: {
          const n = node;
          if (n.name) emitSymbol(out, sf, n, "function", n.name.text, compId);
          break;
        }
        case ts.SyntaxKind.MethodDeclaration:
        case ts.SyntaxKind.MethodSignature: {
          const n = node;
          const name =
            n.name && "text" in n.name ? n.name.text : undefined;
          if (name) emitSymbol(out, sf, n, "method", name, compId);
          break;
        }
      }
      ts.forEachChild(node, visit);
    };
    try {
      visit(sf);
    } catch (err) {
      reportError(sf.fileName, err.message);
    }
  }

  if (outPath) out.end();
}

function emitCallEdges(out, sf, caller, callSite, calleeName) {
  const callerId = symbolIdFor(sf, caller, caller.name?.text ?? "<anonymous>");
  const start = sf.getLineAndCharacterOfPosition(callSite.getStart(sf));
  const data = {
    source_id: callerId,
    target_id: `ts:callee:${calleeName}`,
    kind: "CALLS",
    certainty: "asserted",
    created_by: [SOURCE_NAME],
    metadata: {
      invocation_site: { line: start.line + 1, col: start.character + 1 },
    },
  };
  writeLine(out, envelope("edge", data));
}

function cmdCalls(target, outPath) {
  const program = buildProgram(target);
  const out = openOutput(outPath);

  for (const sf of program.getSourceFiles()) {
    if (sf.isDeclarationFile || sf.fileName.includes("node_modules")) continue;
    const enclosingFn = [];
    const visit = (node) => {
      const isFn =
        node.kind === ts.SyntaxKind.FunctionDeclaration ||
        node.kind === ts.SyntaxKind.MethodDeclaration;
      if (isFn) enclosingFn.push(node);
      if (node.kind === ts.SyntaxKind.CallExpression) {
        const call = node;
        const caller = enclosingFn[enclosingFn.length - 1];
        if (caller) {
          const callee =
            call.expression.kind === ts.SyntaxKind.Identifier
              ? call.expression.text
              : call.expression.getText(sf);
          emitCallEdges(out, sf, caller, call, callee);
        }
      }
      ts.forEachChild(node, visit);
      if (isFn) enclosingFn.pop();
    };
    try {
      visit(sf);
    } catch (err) {
      reportError(sf.fileName, err.message);
    }
  }
  if (outPath) out.end();
}

function emitHttpContract(out, sf, node, method, url) {
  const id = `http.${method.toUpperCase()}.${url}`;
  const contract = {
    id,
    kind: "http_endpoint",
    name: `${method.toUpperCase()} ${url}`,
    spec: { method: method.toUpperCase(), path: url },
    metadata: { detected_by: "ts_fetch_literal" },
    certainty: "inferred",
    created_by: [SOURCE_NAME],
  };
  writeLine(out, envelope("contract", contract));
  const callerId = symbolIdFor(sf, node, "<caller>");
  writeLine(
    out,
    envelope("edge", {
      source_id: callerId,
      target_id: id,
      kind: "CALLS",
      certainty: "inferred",
      created_by: [SOURCE_NAME],
      metadata: {},
    }),
  );
}

function cmdContracts(target, outPath) {
  const program = buildProgram(target);
  const out = openOutput(outPath);

  for (const sf of program.getSourceFiles()) {
    if (sf.isDeclarationFile || sf.fileName.includes("node_modules")) continue;
    const visit = (node) => {
      if (node.kind === ts.SyntaxKind.CallExpression) {
        const call = node;
        const expr = call.expression;
        const text = expr.getText(sf);
        if (
          (text === "fetch" || text.endsWith(".fetch")) &&
          call.arguments[0]?.kind === ts.SyntaxKind.StringLiteral
        ) {
          const url = call.arguments[0].text;
          let method = "GET";
          if (call.arguments[1]?.kind === ts.SyntaxKind.ObjectLiteralExpression) {
            for (const p of call.arguments[1].properties) {
              if (
                p.kind === ts.SyntaxKind.PropertyAssignment &&
                p.name.getText(sf) === "method" &&
                p.initializer.kind === ts.SyntaxKind.StringLiteral
              ) {
                method = p.initializer.text;
              }
            }
          }
          emitHttpContract(out, sf, node, method, url);
        }
      }
      ts.forEachChild(node, visit);
    };
    try {
      visit(sf);
    } catch (err) {
      reportError(sf.fileName, err.message);
    }
  }
  if (outPath) out.end();
}

// ORM / query-builder verbs. The receiver tells us the entity; the
// verb tells us read vs write. Prisma exposes them as
// ``client.<model>.<verb>()``; Sequelize / TypeORM as model statics
// ``<Model>.<verb>()``.
const _DATA_READ_OPS = new Set([
  "find", "findone", "findall", "findmany", "findunique", "findfirst",
  "count", "aggregate", "select",
]);
const _DATA_WRITE_OPS = new Set([
  "create", "createmany", "insert", "update", "updatemany", "updateone",
  "save", "delete", "deletemany", "deleteone", "destroy", "upsert",
  "bulkcreate",
]);

// Raw-SQL table extraction. A literal is only treated as SQL when it
// carries a statement keyword, which keeps ordinary strings out.
const _SQL_HINT = /\b(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE)\b/i;
const _SQL_TABLE = [
  // The READS ``FROM`` rule must not also fire on ``DELETE FROM``,
  // which the WRITES rule below already owns.
  { re: /(?<!DELETE\s{1,4})\bFROM\s+[`"'[]?([A-Za-z_][\w.]*)/gi, access: "READS" },
  { re: /\bJOIN\s+[`"'[]?([A-Za-z_][\w.]*)/gi, access: "READS" },
  { re: /\bINSERT\s+INTO\s+[`"'[]?([A-Za-z_][\w.]*)/gi, access: "WRITES" },
  { re: /\bUPDATE\s+[`"'[]?([A-Za-z_][\w.]*)/gi, access: "WRITES" },
  { re: /\bDELETE\s+FROM\s+[`"'[]?([A-Za-z_][\w.]*)/gi, access: "WRITES" },
];

function dataEntityName(raw) {
  // Strip quoting, drop any schema/owner qualifier (dbo.Orders → orders).
  const cleaned = raw.replace(/[`"'[\]]/g, "").trim().toLowerCase();
  const parts = cleaned.split(".");
  return parts[parts.length - 1];
}

function emitDataAccess(out, sf, fnNode, rawEntity, access, site, seen) {
  const name = dataEntityName(rawEntity);
  // A logical (name-keyed) entity id — the merge layer reconciles it
  // against the schema-qualified DataEntity the DB analyzers emit.
  if (!name || !/^[a-z_]\w*$/.test(name)) return;
  const entityId = `data.${name}`;
  if (!seen.has(entityId)) {
    seen.add(entityId);
    writeLine(
      out,
      envelope("data_entity", {
        id: entityId,
        kind: "table",
        name,
        schema: {},
        sample_available: false,
        is_sensitive: false,
        certainty: "inferred",
        created_by: [SOURCE_NAME],
        metadata: { detected_by: "ts_static" },
      }),
    );
  }
  const callerId = fnNode
    ? symbolIdFor(sf, fnNode, fnNode.name?.text ?? "<anonymous>")
    : `ts:${path.basename(sf.fileName)}:<module>`;
  writeLine(
    out,
    envelope("edge", {
      source_id: callerId,
      target_id: entityId,
      kind: access,
      certainty: "inferred",
      created_by: [SOURCE_NAME],
      metadata: { access_site: site },
    }),
  );
}

function cmdDataAccess(target, outPath) {
  const program = buildProgram(target);
  const out = openOutput(outPath);

  for (const sf of program.getSourceFiles()) {
    if (sf.isDeclarationFile || sf.fileName.includes("node_modules")) continue;
    const enclosingFn = [];
    const seen = new Set();
    const visit = (node) => {
      const isFn =
        node.kind === ts.SyntaxKind.FunctionDeclaration ||
        node.kind === ts.SyntaxKind.MethodDeclaration;
      if (isFn) enclosingFn.push(node);
      const fn = enclosingFn[enclosingFn.length - 1] ?? null;

      // (1) Raw SQL embedded in string / template literals.
      if (
        node.kind === ts.SyntaxKind.StringLiteral ||
        node.kind === ts.SyntaxKind.NoSubstitutionTemplateLiteral ||
        node.kind === ts.SyntaxKind.TemplateExpression
      ) {
        const text = node.getText(sf);
        if (_SQL_HINT.test(text)) {
          const s = sf.getLineAndCharacterOfPosition(node.getStart(sf));
          const site = { line: s.line + 1, col: s.character + 1 };
          for (const { re, access } of _SQL_TABLE) {
            re.lastIndex = 0;
            let m;
            while ((m = re.exec(text)) !== null) {
              emitDataAccess(out, sf, fn, m[1], access, site, seen);
            }
          }
        }
      }

      // (2) ORM-style method calls — prisma.<model>.<verb>() or
      //     <Model>.<verb>() statics.
      if (
        node.kind === ts.SyntaxKind.CallExpression &&
        node.expression.kind === ts.SyntaxKind.PropertyAccessExpression
      ) {
        const expr = node.expression;
        const op = expr.name.text.toLowerCase();
        const isWrite = _DATA_WRITE_OPS.has(op);
        const isRead = _DATA_READ_OPS.has(op);
        if (isRead || isWrite) {
          const recv = expr.expression;
          let entity = null;
          if (recv.kind === ts.SyntaxKind.PropertyAccessExpression) {
            // client.<model>.<verb>()
            entity = recv.name.text;
          } else if (
            recv.kind === ts.SyntaxKind.Identifier &&
            /^[A-Z]/.test(recv.text)
          ) {
            // <Model>.<verb>() — gated on the capitalised-model
            // convention so ordinary helper calls don't leak in.
            entity = recv.text;
          }
          if (entity) {
            const s = sf.getLineAndCharacterOfPosition(node.getStart(sf));
            emitDataAccess(out, sf, fn, entity, isWrite ? "WRITES" : "READS",
              { line: s.line + 1, col: s.character + 1 }, seen);
          }
        }
      }

      ts.forEachChild(node, visit);
      if (isFn) enclosingFn.pop();
    };
    try {
      visit(sf);
    } catch (err) {
      reportError(sf.fileName, err.message);
    }
  }
  if (outPath) out.end();
}

function cmdSchema() {
  return {
    schema: "https://mnemos.dev/analyzer/ggoss-ts/v1",
    record_types: ["symbol", "contract", "data_entity", "edge"],
    emits_edges: ["CALLS", "READS", "WRITES"],
  };
}

async function main() {
  const [verb, ...rest] = process.argv.slice(2);
  if (!verb) {
    process.stderr.write(
      "usage: ggoss-ts <probe|inventory|symbols|calls|contracts|data_access|schema> [args]\n",
    );
    process.exit(2);
  }

  const { target, outPath } = parseCommon(rest);

  try {
    switch (verb) {
      case "probe":
        process.stdout.write(JSON.stringify(cmdProbe(target)) + "\n");
        return;
      case "inventory":
        process.stdout.write(JSON.stringify(cmdInventory(target)) + "\n");
        return;
      case "symbols":
        cmdSymbols(target, outPath);
        return;
      case "calls":
        cmdCalls(target, outPath);
        return;
      case "contracts":
        cmdContracts(target, outPath);
        return;
      case "data_access":
        cmdDataAccess(target, outPath);
        return;
      case "schema":
        process.stdout.write(JSON.stringify(cmdSchema()) + "\n");
        return;
      default:
        process.stderr.write(`unknown verb: ${verb}\n`);
        process.exit(2);
    }
  } catch (err) {
    process.stderr.write(
      JSON.stringify({
        level: "error",
        message: err.message,
        recoverable: false,
      }) + "\n",
    );
    process.exit(1);
  }
}

await main();
