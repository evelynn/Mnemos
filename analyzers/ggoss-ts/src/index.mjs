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

const _SOURCE_EXTS = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"];

// Generated-output directories — never source, always skipped.
const _SKIP_DIRS = new Set(["node_modules", "dist", "build", "coverage"]);

// Test / fixture directory names. Normally analysed like any other
// code, but excluded on the crash-retry path: a compiler-style repo
// keeps intentionally-malformed files under these and they can trip a
// hard assertion inside the TypeScript program builder.
const _TEST_DIRS = new Set([
  "tests", "test", "__tests__", "fixtures", "__fixtures__",
  "spec", "specs", "e2e",
]);

function walkFiles(dir, exts, opts = {}, collected = []) {
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch (err) {
    reportError(dir, err.message);
    return collected;
  }
  for (const e of entries) {
    if (e.name.startsWith(".") || _SKIP_DIRS.has(e.name)) continue;
    if (opts.skipTests && _TEST_DIRS.has(e.name)) continue;
    const full = path.join(dir, e.name);
    if (e.isDirectory()) walkFiles(full, exts, opts, collected);
    else if (exts.some((ext) => e.name.endsWith(ext))) collected.push(full);
  }
  return collected;
}

function cmdProbe(target) {
  if (!target || !fs.existsSync(target)) {
    return { applicable: false, reason: "path_not_found", files_found: 0 };
  }
  const files = walkFiles(target, _SOURCE_EXTS);
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
  const files = walkFiles(target, _SOURCE_EXTS);
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

const _PROGRAM_OPTS = {
  allowJs: true,
  checkJs: false,
  target: ts.ScriptTarget.ES2022,
  module: ts.ModuleKind.ESNext,
  // Bundler resolution is lenient enough that ``from "./lib"`` works
  // without a ``.js`` suffix — important because the TypeChecker can
  // then follow import aliases and CALLS edges resolve to the real
  // declaration in lib.ts instead of an "unknown" alias dead-end.
  moduleResolution: ts.ModuleResolutionKind.Bundler ?? ts.ModuleResolutionKind.NodeNext,
  jsx: ts.JsxEmit.Preserve,
  noEmit: true,
  skipLibCheck: true,
};

function buildProgram(target) {
  // An analyzer must see *all* the code, not the subset a project's
  // build tsconfig happens to scope. Real repos make that distinction
  // bite: astro's root tsconfig is solution-style (project references,
  // ~0 files), and next.js' root tsconfig ``include``s only its test
  // suite — trusting either analyses the wrong thing. So the file set
  // always comes from a directory walk; a tsconfig contributes only
  // its compilerOptions (jsx / paths / target).
  let options = { ..._PROGRAM_OPTS };
  const tsconfigPath = path.join(target, "tsconfig.json");
  if (fs.existsSync(tsconfigPath)) {
    try {
      const raw = ts.readConfigFile(tsconfigPath, ts.sys.readFile);
      const parsed = ts.parseJsonConfigFileContent(
        raw.config || {}, ts.sys, target,
      );
      // _PROGRAM_OPTS wins for the flags analysis depends on (noEmit,
      // allowJs, skipLibCheck); the tsconfig keeps jsx / paths / target.
      options = { ...parsed.options, ..._PROGRAM_OPTS };
    } catch {
      // A malformed tsconfig is not fatal — the defaults analyse fine.
    }
  }

  const files = walkFiles(target, _SOURCE_EXTS);
  try {
    return ts.createProgram({ rootNames: files, options });
  } catch (err) {
    // A pathological source file — common in a compiler's own test
    // corpus — can trip a hard assertion inside createProgram. Retry
    // once without test / fixture directories so a normal codebase
    // still yields a result instead of a zero-output crash.
    reportError(
      target,
      `program build failed (${err.message}); retrying without test dirs`,
      true,
    );
    const safe = walkFiles(target, _SOURCE_EXTS, { skipTests: true });
    return ts.createProgram({ rootNames: safe, options });
  }
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
        case ts.SyntaxKind.VariableDeclaration: {
          // Modern JS/TS exports functions as ``const f = () => {}`` /
          // ``const f = function () {}`` — not FunctionDeclaration. A
          // 10-project benchmark showed missing this loses most of a
          // codebase's symbols (next.js: 1.7k from 21k files).
          const n = node;
          const init = n.initializer;
          if (
            n.name &&
            n.name.kind === ts.SyntaxKind.Identifier &&
            init &&
            (init.kind === ts.SyntaxKind.ArrowFunction ||
              init.kind === ts.SyntaxKind.FunctionExpression ||
              init.kind === ts.SyntaxKind.ClassExpression)
          ) {
            const kind =
              init.kind === ts.SyntaxKind.ClassExpression ? "class" : "function";
            emitSymbol(out, sf, n, kind, n.name.text, compId);
          }
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

function _resolveCalleeId(checker, callExpr) {
  // Resolve the call's expression back to the declaring symbol via the
  // TypeChecker and produce the same ``ts:<basename>:<name>@L:C`` id
  // ``cmdSymbols`` emits — so CALLS edges actually JOIN with symbol
  // nodes. Returns null for unresolved / external / built-in calls.
  try {
    let sym = checker.getSymbolAtLocation(callExpr.expression);
    if (!sym) return null;
    // Follow import aliases to the original declaration — otherwise
    // ``ts:main.ts:helper@…`` (the import binding) is returned instead
    // of ``ts:lib.ts:helper@…`` (the real function).
    if (sym.flags & ts.SymbolFlags.Alias) {
      try { sym = checker.getAliasedSymbol(sym); } catch { /* keep sym */ }
    }
    for (const decl of sym.declarations || []) {
      const declSf = decl.getSourceFile && decl.getSourceFile();
      if (!declSf || declSf.isDeclarationFile) continue;
      if (declSf.fileName.includes("node_modules")) continue;
      // FunctionDeclaration / MethodDeclaration / VariableDeclaration
      // with a plain identifier name — the same shapes cmdSymbols
      // emits as symbols.
      const nm = decl.name;
      if (nm && nm.kind === ts.SyntaxKind.Identifier) {
        return symbolIdFor(declSf, decl, nm.text);
      }
    }
    return null;
  } catch {
    return null;
  }
}

function emitCallEdges(out, sf, caller, callSite, calleeId, calleeName, callerName) {
  // ``callerName`` is the binding name for ArrowFunction /
  // FunctionExpression where ``caller.name`` is undefined. Passed
  // by cmdCalls; FunctionDeclaration / MethodDeclaration callers
  // pass their own ``.name.text``.
  const _name = callerName ?? caller.name?.text ?? "<anonymous>";
  const callerId = symbolIdFor(sf, caller, _name);
  const start = sf.getLineAndCharacterOfPosition(callSite.getStart(sf));
  // Resolved callee → joinable symbol id, asserted certainty.
  // Unresolved → ``ts:extern:<name>`` so external/built-in calls are
  // distinct from intra-project ones and clearly inferred.
  const resolved = calleeId != null;
  const data = {
    source_id: callerId,
    target_id: resolved ? calleeId : `ts:extern:${calleeName}`,
    kind: "CALLS",
    certainty: resolved ? "asserted" : "inferred",
    created_by: [SOURCE_NAME],
    metadata: {
      invocation_site: { line: start.line + 1, col: start.character + 1 },
      callee_resolved: resolved,
    },
  };
  writeLine(out, envelope("edge", data));
}

function cmdCalls(target, outPath) {
  const program = buildProgram(target);
  const checker = program.getTypeChecker();
  const out = openOutput(outPath);

  for (const sf of program.getSourceFiles()) {
    if (sf.isDeclarationFile || sf.fileName.includes("node_modules")) continue;
    // Each entry: { node, nameForId } — name is what emitCallEdges
    // uses to build the caller symbol id. For FunctionDeclaration /
    // MethodDeclaration, ``node.name.text`` exists; for ArrowFunction
    // / FunctionExpression assigned to a const, we look one parent
    // up at the VariableDeclaration. Without this, modern JS code
    // (``const f = () => g()``) silently dropped every callee — a
    // regression PR-111's accuracy harness flagged at recall 0.667.
    const enclosingFn = [];
    const _enclosingNameFor = (node) => {
      if (node.kind === ts.SyntaxKind.FunctionDeclaration ||
          node.kind === ts.SyntaxKind.MethodDeclaration) {
        return node.name?.text ?? "<anonymous>";
      }
      // ArrowFunction / FunctionExpression: name is on the parent
      // VariableDeclaration (``const X = (...) => ...``) or
      // PropertyAssignment (``{ X: (...) => ... }``).
      let p = node.parent;
      while (p) {
        if (p.kind === ts.SyntaxKind.VariableDeclaration && p.name?.text) {
          return p.name.text;
        }
        if (p.kind === ts.SyntaxKind.PropertyAssignment && p.name?.text) {
          return p.name.text;
        }
        // Stop walking at the next function — beyond that we're
        // outside the binding scope.
        if (p.kind === ts.SyntaxKind.FunctionDeclaration ||
            p.kind === ts.SyntaxKind.MethodDeclaration ||
            p.kind === ts.SyntaxKind.ArrowFunction ||
            p.kind === ts.SyntaxKind.FunctionExpression) {
          break;
        }
        p = p.parent;
      }
      return null;  // anonymous IIFE etc — we still track scope but emit nothing
    };
    const visit = (node) => {
      const isFn =
        node.kind === ts.SyntaxKind.FunctionDeclaration ||
        node.kind === ts.SyntaxKind.MethodDeclaration ||
        node.kind === ts.SyntaxKind.ArrowFunction ||
        node.kind === ts.SyntaxKind.FunctionExpression;
      if (isFn) {
        enclosingFn.push({ node, nameForId: _enclosingNameFor(node) });
      }
      if (node.kind === ts.SyntaxKind.CallExpression) {
        const call = node;
        const caller = enclosingFn[enclosingFn.length - 1];
        if (caller && caller.nameForId) {
          const calleeName =
            call.expression.kind === ts.SyntaxKind.Identifier
              ? call.expression.text
              : call.expression.getText(sf);
          const calleeId = _resolveCalleeId(checker, call);
          emitCallEdges(
            out, sf, caller.node, call, calleeId, calleeName,
            caller.nameForId,
          );
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

// HTTP verbs an Express/Koa-style router exposes as ``router.<verb>()``.
const _HTTP_VERBS = new Set([
  "get", "post", "put", "delete", "patch", "options", "head", "all",
]);
// NestJS method decorators that declare a route.
const _HTTP_DECORATORS = new Set([
  "Get", "Post", "Put", "Delete", "Patch", "Options", "Head", "All",
]);

// Next.js App Router: a file ``app/**/route.{ts,tsx,js,mjs}`` that exports a
// function named after an HTTP method (``export async function POST``) serves
// that method at the URL derived from its directory. Previously ggoss-ts saw
// the client ``fetch('/api/..')`` (CALLS) but never the server handler
// (EXPOSES), so the contract had no exposer and duplicate-endpoint detection /
// OTLP runtime reconcile could not work for Next.js apps.
const _NEXTJS_METHODS = new Set([
  "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
]);

function _nextjsRoutePath(fileName) {
  const norm = fileName.replace(/\\/g, "/");
  if (/\/app\/route\.(?:ts|tsx|js|mjs)$/.test(norm)) return "/";
  const m = norm.match(/\/app\/(.+)\/route\.(?:ts|tsx|js|mjs)$/);
  if (!m) return null;
  const segs = m[1]
    .split("/")
    .filter(Boolean)
    // Route groups ``(group)`` and parallel/intercepting routes carry no URL.
    .filter((s) => !(s.startsWith("(") && s.endsWith(")")) && !s.startsWith("@"))
    // Dynamic segments ``[id]`` / ``[...slug]`` → ``{id}`` / ``{slug}``.
    .map((s) => s.replace(/^\[\.{3}(.+)\]$/, "{$1}").replace(/^\[(.+)\]$/, "{$1}"));
  return "/" + segs.join("/");
}

function _exportedHttpHandler(node) {
  const exported = (node.modifiers || []).some(
    (m) => m.kind === ts.SyntaxKind.ExportKeyword,
  );
  if (!exported) return null;
  // export [async] function GET(...) {}
  if (
    node.kind === ts.SyntaxKind.FunctionDeclaration &&
    node.name &&
    _NEXTJS_METHODS.has(node.name.text)
  ) {
    return node.name.text;
  }
  // export const GET = (...) => {}
  if (node.kind === ts.SyntaxKind.VariableStatement) {
    for (const d of node.declarationList.declarations) {
      if (
        d.name &&
        d.name.kind === ts.SyntaxKind.Identifier &&
        _NEXTJS_METHODS.has(d.name.text) &&
        d.initializer
      ) {
        return d.name.text;
      }
    }
  }
  return null;
}

function _decorators(node) {
  if (typeof ts.getDecorators === "function") {
    return ts.getDecorators(node) || [];
  }
  return (node.modifiers || []).filter(
    (m) => m.kind === ts.SyntaxKind.Decorator,
  );
}

function _decoratorInfo(dec, sf) {
  // Return { name, arg } for @Name or @Name("arg"); arg is the first
  // string-literal argument or null.
  const e = dec.expression;
  if (e.kind === ts.SyntaxKind.Identifier) return { name: e.text, arg: null };
  if (
    e.kind === ts.SyntaxKind.CallExpression &&
    e.expression.kind === ts.SyntaxKind.Identifier
  ) {
    const a0 = e.arguments[0];
    const arg =
      a0 && a0.kind === ts.SyntaxKind.StringLiteral ? a0.text : null;
    return { name: e.expression.text, arg };
  }
  return null;
}

function _joinRoute(prefix, route) {
  const parts = [prefix, route].filter((p) => p && p !== "/");
  return ("/" + parts.join("/")).replace(/\/+/g, "/");
}

function _normalizeUrl(url) {
  // Drop query string and fragment — they are call-site data, not part
  // of the contract identity. An absolute URL (an external dependency)
  // keeps its scheme+host; a relative one gets a single leading slash.
  const bare = url.split("?")[0].split("#")[0];
  if (/^https?:\/\//i.test(bare)) return bare;
  return bare.startsWith("/") ? bare : "/" + bare;
}

function emitHttpContract(out, sf, node, method, url, relation, detectedBy) {
  const path_ = _normalizeUrl(url);
  const id = `http.${method.toUpperCase()}.${path_}`;
  const contract = {
    id,
    kind: "http_endpoint",
    name: `${method.toUpperCase()} ${path_}`,
    spec: { method: method.toUpperCase(), path: path_ },
    metadata: { detected_by: detectedBy },
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
      // A server route EXPOSES the contract; a client fetch CALLS it.
      kind: relation,
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

    // Next.js App Router route handlers — detected per-file from the path +
    // exported HTTP-method functions, not via the AST verb/decorator visit.
    const nextRoute = _nextjsRoutePath(sf.fileName);
    if (nextRoute !== null) {
      for (const stmt of sf.statements) {
        const method = _exportedHttpHandler(stmt);
        if (method) {
          emitHttpContract(
            out, sf, stmt, method, nextRoute, "EXPOSES", "ts_nextjs_route",
          );
        }
      }
    }

    // ``prefix`` carries a NestJS @Controller('x') route prefix down
    // into the class's methods.
    const visit = (node, prefix) => {
      let childPrefix = prefix;

      // NestJS @Controller('cats') — sets the prefix for child methods.
      if (node.kind === ts.SyntaxKind.ClassDeclaration) {
        for (const dec of _decorators(node)) {
          const info = _decoratorInfo(dec, sf);
          if (info && info.name === "Controller") {
            childPrefix = info.arg || "";
          }
        }
      }

      // NestJS @Get(':id') etc. on a method — the server EXPOSES it.
      if (node.kind === ts.SyntaxKind.MethodDeclaration) {
        for (const dec of _decorators(node)) {
          const info = _decoratorInfo(dec, sf);
          if (info && _HTTP_DECORATORS.has(info.name)) {
            const route = _joinRoute(prefix, info.arg || "");
            emitHttpContract(
              out, sf, node, info.name, route, "EXPOSES",
              "ts_nest_decorator",
            );
          }
        }
      }

      if (node.kind === ts.SyntaxKind.CallExpression) {
        const call = node;
        const expr = call.expression;
        const text = expr.getText(sf);
        if (
          (text === "fetch" || text.endsWith(".fetch")) &&
          call.arguments[0]?.kind === ts.SyntaxKind.StringLiteral
        ) {
          // Client side — this code CALLS the contract.
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
          emitHttpContract(
            out, sf, node, method, url, "CALLS", "ts_fetch_literal",
          );
        } else if (expr.kind === ts.SyntaxKind.PropertyAccessExpression) {
          // Express / Koa router — ``app.get("/path", handler)``. The
          // path must start with "/" so ``map.get("key")`` and other
          // ordinary ``.get()`` calls don't masquerade as routes.
          const verb = expr.name.text.toLowerCase();
          const a0 = call.arguments[0];
          if (
            _HTTP_VERBS.has(verb) &&
            a0?.kind === ts.SyntaxKind.StringLiteral &&
            a0.text.startsWith("/")
          ) {
            emitHttpContract(
              out, sf, node, verb, a0.text, "EXPOSES",
              "ts_express_route",
            );
          }
        }
      }
      ts.forEachChild(node, (c) => visit(c, childPrefix));
    };
    try {
      visit(sf, "");
    } catch (err) {
      reportError(sf.fileName, err.message);
    }
  }
  if (outPath) out.end();
}

// Raw-SQL table extraction. ``_looksLikeSql`` is deliberately strict:
// running it against real codebases showed a bare keyword ("update
// the cache", "select an option") floods the graph with junk
// entities, so a string is only treated as SQL when it *begins* with
// a statement and carries that statement's mandatory clause.
const _SQL_TABLE = [
  // The READS ``FROM`` rule must not also fire on ``DELETE FROM``,
  // which the WRITES rule below already owns.
  { re: /(?<!DELETE\s{1,4})\bFROM\s+[`"'[]?([A-Za-z_][\w.]*)/gi, access: "READS" },
  { re: /\bJOIN\s+[`"'[]?([A-Za-z_][\w.]*)/gi, access: "READS" },
  { re: /\bINSERT\s+INTO\s+[`"'[]?([A-Za-z_][\w.]*)/gi, access: "WRITES" },
  { re: /\bUPDATE\s+[`"'[]?([A-Za-z_][\w.]*)/gi, access: "WRITES" },
  { re: /\bDELETE\s+FROM\s+[`"'[]?([A-Za-z_][\w.]*)/gi, access: "WRITES" },
];

function _looksLikeSql(raw) {
  // Drop the JS quote/backtick wrapper and any leading whitespace.
  const s = raw.replace(/^[`'"\s]+/, "");
  const head = s.slice(0, 14).toUpperCase();
  if (head.startsWith("SELECT ")) return /\bFROM\b/i.test(s);
  if (head.startsWith("UPDATE ")) return /\bSET\b/i.test(s);
  if (head.startsWith("WITH ")) return /\bSELECT\b/i.test(s) && /\bFROM\b/i.test(s);
  return (
    head.startsWith("INSERT INTO ") ||
    head.startsWith("DELETE FROM ") ||
    head.startsWith("MERGE INTO ")
  );
}

// JS built-ins whose statics collide with ORM verbs — ``Object.create``
// is not a database write. Excluded from the capitalised-model branch.
const _JS_BUILTINS = new Set([
  "Object", "Array", "Map", "Set", "WeakMap", "WeakSet", "Promise",
  "Math", "JSON", "Number", "String", "Boolean", "Symbol", "BigInt",
  "Date", "RegExp", "Error", "Reflect", "Proxy", "console", "Intl",
]);

// Identifier names that mark a chain as a genuine ORM/DB client. Kept
// deliberately tight after real-world testing: a loose list collides
// (Angular Material's ``dataSource`` is not a database, a ``pool`` may
// be a worker pool). The Prisma ``client.model.verb()`` shape rooted
// in one of these is high-precision.
const _ORM_CLIENT = /^(prisma|prismaclient|db|dbclient|knex|orm|sequelize)$/i;

// Functions whose string argument is genuinely SQL. A string is only
// scanned for tables when it is passed to one of these (or a ``sql``
// tagged template) — a bare string literal that merely starts with
// "Update ..." is prose, not a query.
const _SQL_EXEC_FNS = new Set([
  "query", "execute", "exec", "raw", "prepare", "unsafe",
]);

const _STRINGY = new Set([
  ts.SyntaxKind.StringLiteral,
  ts.SyntaxKind.NoSubstitutionTemplateLiteral,
  ts.SyntaxKind.TemplateExpression,
]);

function _chainHasOrmClient(node) {
  // Walk a property-access / call chain collecting identifier names;
  // true iff any of them looks like a DB client.
  let cur = node;
  while (cur) {
    if (cur.kind === ts.SyntaxKind.Identifier) {
      return _ORM_CLIENT.test(cur.text);
    }
    if (cur.kind === ts.SyntaxKind.PropertyAccessExpression) {
      if (_ORM_CLIENT.test(cur.name.text)) return true;
      cur = cur.expression;
    } else if (cur.kind === ts.SyntaxKind.CallExpression) {
      cur = cur.expression;
    } else {
      return false;
    }
  }
  return false;
}

// ORM / query-builder verbs. The receiver tells us the entity; the
// verb tells us read vs write.
const _DATA_READ_OPS = new Set([
  "find", "findone", "findall", "findmany", "findunique", "findfirst",
  "count", "aggregate", "select",
]);
const _DATA_WRITE_OPS = new Set([
  "create", "createmany", "insert", "update", "updatemany", "updateone",
  "save", "delete", "deletemany", "deleteone", "destroy", "upsert",
  "bulkcreate",
]);

// ORM-distinctive verbs — these rarely collide with ordinary code, so
// they are the only ones trusted on the *un-client-gated* capitalised-
// model branch. ``create`` / ``find`` / ``update`` are too generic
// there (``NestFactory.create()`` is not a database write); on the
// client-gated Prisma branch the client itself is the precision
// guarantee, so all verbs are allowed there.
const _DISTINCTIVE_OPS = new Set([
  "findall", "findmany", "findunique", "findfirst", "findone",
  "createmany", "updatemany", "updateone", "deletemany", "deleteone",
  "bulkcreate", "upsert", "aggregate",
]);

function dataEntityName(raw) {
  // Strip quoting, drop any schema/owner qualifier (dbo.Orders → orders).
  const cleaned = raw.replace(/[`"'[\]]/g, "").trim().toLowerCase();
  const parts = cleaned.split(".");
  return parts[parts.length - 1];
}

// ORM infrastructure names — never a real table. A ``db.connection.
// find()`` chain would otherwise emit ``data.connection``.
const _NOT_ENTITY = new Set([
  "connection", "connections", "entitymanager", "manager", "client",
  "pool", "transaction", "trx", "queryrunner", "querybuilder",
  "repository", "repositories", "datasource",
]);

function emitDataAccess(out, sf, fnNode, rawEntity, access, site, seen) {
  const name = dataEntityName(rawEntity);
  // A logical (name-keyed) entity id — the merge layer reconciles it
  // against the schema-qualified DataEntity the DB analyzers emit.
  if (!name || !/^[a-z_]\w*$/.test(name)) return;
  if (_NOT_ENTITY.has(name)) return;
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

function _scanSqlText(out, sf, fn, text, node, seen) {
  if (!_looksLikeSql(text)) return;
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

      // (1) Raw SQL in a ``sql`...` `` tagged template.
      if (
        node.kind === ts.SyntaxKind.TaggedTemplateExpression &&
        node.tag &&
        /(^|\.)sql$/i.test(node.tag.getText(sf))
      ) {
        _scanSqlText(out, sf, fn, node.template.getText(sf), node, seen);
      }

      // (2) Raw SQL passed to a SQL-executing call — db.query("..."),
      //     conn.execute("..."), exec("..."). A bare string literal
      //     that merely starts with "Update ..." is prose, not a query,
      //     so only these call sites are scanned.
      if (node.kind === ts.SyntaxKind.CallExpression) {
        const callee = node.expression;
        let calleeName = null;
        if (callee.kind === ts.SyntaxKind.PropertyAccessExpression) {
          calleeName = callee.name.text.toLowerCase();
        } else if (callee.kind === ts.SyntaxKind.Identifier) {
          calleeName = callee.text.toLowerCase();
        }
        if (calleeName && _SQL_EXEC_FNS.has(calleeName)) {
          for (const arg of node.arguments) {
            if (_STRINGY.has(arg.kind)) {
              _scanSqlText(out, sf, fn, arg.getText(sf), arg, seen);
            }
          }
        }
      }

      // (3) ORM-style method calls. Two shapes, each gated to keep
      //     ordinary JS out: prisma.<model>.<verb>() — only when the
      //     chain roots in a recognised DB client — and <Model>.<verb>()
      //     statics — only for non-built-in capitalised receivers.
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
          if (
            recv.kind === ts.SyntaxKind.PropertyAccessExpression &&
            _chainHasOrmClient(recv.expression)
          ) {
            // client.<model>.<verb>() — the chain roots in a DB client.
            entity = recv.name.text;
          } else if (
            recv.kind === ts.SyntaxKind.Identifier &&
            /^[A-Z]/.test(recv.text) &&
            !_JS_BUILTINS.has(recv.text) &&
            _DISTINCTIVE_OPS.has(op)
          ) {
            // <Model>.<verb>() — capitalised, not a JS built-in, and
            // an ORM-distinctive verb (so NestFactory.create() and
            // other capitalised factories don't leak in).
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
    emits_edges: ["CALLS", "READS", "WRITES", "EXPOSES"],
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
