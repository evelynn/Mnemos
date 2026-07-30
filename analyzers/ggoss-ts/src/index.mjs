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
let _analysisRoot = process.cwd();

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

const _SOURCE_EXTS = [
  ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue",
];

// Generated-output directories — never source, always skipped.
const _SKIP_DIRS = new Set([
  "node_modules", "dist", "build", "coverage", "out",
]);

// Minified / bundled output (e.g. a vendored ``a2ui.bundle.js``) — generated,
// not source. A single bundle can be tens of thousands of unreadable nodes
// that swamp the graph, so it is skipped by filename (PR-183 S2).
const _SKIP_FILE_RE =
  /(?:\.(?:min|bundle|iife|umd)|^(?:bundle|iife|umd))\.(?:[cm]?js)$/i;

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
    if (fs.lstatSync(dir).isSymbolicLink()) return collected;
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch (err) {
    reportError(dir, err.message);
    return collected;
  }
  for (const e of entries) {
    if (e.isSymbolicLink()) continue;
    if (e.name.startsWith(".") || _SKIP_DIRS.has(e.name)) continue;
    if (opts.skipTests && _TEST_DIRS.has(e.name)) continue;
    const full = path.join(dir, e.name);
    if (e.isDirectory()) walkFiles(full, exts, opts, collected);
    else if (
      exts.some((ext) => e.name.toLowerCase().endsWith(ext)) &&
      !_SKIP_FILE_RE.test(e.name)
    )
      collected.push(full);
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
    return `svc.${pkg.name ?? process.env.MNEMOS_PROJECT_ID ?? path.basename(target)}`;
  } catch {
    return `svc.${process.env.MNEMOS_PROJECT_ID ?? path.basename(target)}`;
  }
}

function sourceRelative(fileName) {
  const rel = path.relative(_analysisRoot, path.resolve(fileName));
  return rel.split(path.sep).join("/");
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

function buildOptions(target) {
  // _PROGRAM_OPTS wins for the flags analysis depends on (noEmit, allowJs,
  // skipLibCheck); a project tsconfig contributes the rest (jsx / paths /
  // target). A malformed tsconfig is not fatal — the defaults analyse fine.
  let options = { ..._PROGRAM_OPTS };
  const tsconfigPath = path.join(target, "tsconfig.json");
  if (fs.existsSync(tsconfigPath)) {
    try {
      const raw = ts.readConfigFile(tsconfigPath, ts.sys.readFile);
      const parsed = ts.parseJsonConfigFileContent(
        raw.config || {}, ts.sys, target,
      );
      options = { ...parsed.options, ..._PROGRAM_OPTS };
    } catch {
      // defaults analyse fine
    }
  }
  return options;
}

function _findTagEnd(source, start) {
  let quote = null;
  for (let i = start; i < source.length; i++) {
    const ch = source[i];
    if (quote !== null) {
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'") quote = ch;
    else if (ch === ">") return i;
  }
  return -1;
}

function _vueScriptSource(fileName) {
  // Vue SFC template/style markup is presentation, not executable source.
  // Preserve newlines and absolute offsets while exposing only inline
  // <script> / <script setup> bodies to the TypeScript parser. This keeps
  // graph locations anchored to the original .vue file without generating
  // or writing a transformed copy into the target repository.
  const source = fs.readFileSync(fileName, "utf-8");
  let masked = source.replace(/[^\r\n]/g, " ");
  const lower = source.toLowerCase();
  let scriptKind = ts.ScriptKind.TS;
  let cursor = 0;
  let depth = 0;
  const voidElements = new Set([
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
  ]);
  while (cursor < source.length) {
    const tagStart = source.indexOf("<", cursor);
    if (tagStart < 0) break;
    if (source.startsWith("<!--", tagStart)) {
      const commentEnd = source.indexOf("-->", tagStart + 4);
      if (commentEnd < 0) throw new Error("unterminated HTML comment");
      cursor = commentEnd + 3;
      continue;
    }
    const tagMatch = source
      .slice(tagStart)
      .match(/^<\s*(\/?)\s*([A-Za-z][\w:-]*)/);
    if (!tagMatch) {
      cursor = tagStart + 1;
      continue;
    }
    const closing = tagMatch[1] === "/";
    const tagName = tagMatch[2].toLowerCase();
    const attributesStart = tagStart + tagMatch[0].length;
    const tagEnd = _findTagEnd(source, attributesStart);
    if (tagEnd < 0) throw new Error(`unterminated <${tagName}> tag`);
    if (closing) {
      depth = Math.max(0, depth - 1);
      cursor = tagEnd + 1;
      continue;
    }
    const rawTag = source.slice(tagStart, tagEnd + 1);
    const selfClosing = /\/\s*>$/.test(rawTag);

    // Script/style elements use raw-text HTML parsing. At SFC top level,
    // restore script bytes; inside <template> they remain presentation and
    // are skipped as one raw element.
    if (tagName !== "script" && tagName !== "style") {
      if (!selfClosing && !voidElements.has(tagName)) depth += 1;
      cursor = tagEnd + 1;
      continue;
    }
    if (selfClosing) {
      cursor = tagEnd + 1;
      continue;
    }
    const closeNeedle = `</${tagName}`;
    const closeStart = lower.indexOf(closeNeedle, tagEnd + 1);
    if (closeStart < 0) throw new Error(`missing </${tagName}> tag`);
    const closeEnd = _findTagEnd(source, closeStart + closeNeedle.length);
    if (closeEnd < 0) {
      throw new Error(`unterminated </${tagName}> tag`);
    }
    const attributes = source.slice(attributesStart, tagEnd);
    // A src-backed block points at a normal JS/TS file which the directory
    // walk indexes independently. Do not fabricate an empty duplicate.
    if (
      tagName === "script" &&
      depth === 0 &&
      !/\bsrc\s*=/i.test(attributes)
    ) {
      const contentStart = tagEnd + 1;
      const content = source.slice(contentStart, closeStart);
      masked =
        masked.slice(0, contentStart) +
        content +
        masked.slice(closeStart);
      if (/\blang\s*=\s*["']?(?:tsx|jsx)\b/i.test(attributes)) {
        scriptKind = ts.ScriptKind.TSX;
      }
    }
    cursor = closeEnd + 1;
  }
  return { text: masked, scriptKind };
}

function _compilerHost(options) {
  const host = ts.createCompilerHost(options, true);
  const baseGetSourceFile = host.getSourceFile.bind(host);
  const vueCache = new Map();
  host.getSourceFile = (
    fileName,
    languageVersion,
    onError,
    shouldCreateNewSourceFile,
  ) => {
    if (!fileName.toLowerCase().endsWith(".vue")) {
      return baseGetSourceFile(
        fileName,
        languageVersion,
        onError,
        shouldCreateNewSourceFile,
      );
    }
    try {
      let parsed = vueCache.get(fileName);
      if (!parsed || shouldCreateNewSourceFile) {
        parsed = _vueScriptSource(fileName);
        vueCache.set(fileName, parsed);
      }
      return ts.createSourceFile(
        fileName,
        parsed.text,
        languageVersion,
        true,
        parsed.scriptKind,
      );
    } catch (err) {
      reportError(fileName, `vue_sfc_parse_failed: ${err.message}`, true);
      if (onError) onError(err.message);
      let raw = "";
      try {
        raw = fs.readFileSync(fileName, "utf-8");
      } catch {
        // The durable stderr category remains generic at ingest.
      }
      const blank = raw.replace(/[^\r\n]/g, " ");
      return ts.createSourceFile(
        fileName,
        blank,
        languageVersion,
        true,
        ts.ScriptKind.TS,
      );
    }
  };
  host.resolveModuleNames = (moduleNames, containingFile) =>
    moduleNames.map((moduleName) => {
      if (moduleName.toLowerCase().endsWith(".vue")) {
        const candidate = path.resolve(path.dirname(containingFile), moduleName);
        if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
          return {
            resolvedFileName: candidate,
            extension: ts.Extension.Ts,
            isExternalLibraryImport: false,
          };
        }
      }
      return ts.resolveModuleName(
        moduleName,
        containingFile,
        options,
        host,
      ).resolvedModule;
    });
  return host;
}

function _createProgram(files, options, analysisAllowedFiles = files) {
  const program = ts.createProgram({
    rootNames: files,
    options: { ...options, allowNonTsExtensions: true },
    host: _compilerHost({ ...options, allowNonTsExtensions: true }),
  });
  // TypeScript follows imports beyond rootNames. Facts must still come only
  // from the analyzer's explicit discovery set; otherwise an excluded bundle
  // can re-enter through ``import "./vendor.iife.js"`` and drift from the
  // source manifest.
  program.__mnemosAllowedFiles = new Set(analysisAllowedFiles.map(_normKey));
  return program;
}

function _programOwnsSource(program, sourceFile) {
  return (
    !sourceFile.isDeclarationFile &&
    program.__mnemosAllowedFiles.has(_normKey(sourceFile.fileName))
  );
}

function buildProgram(target) {
  // An analyzer must see *all* the code, not the subset a project's
  // build tsconfig happens to scope. Real repos make that distinction
  // bite: astro's root tsconfig is solution-style (project references,
  // ~0 files), and next.js' root tsconfig ``include``s only its test
  // suite — trusting either analyses the wrong thing. So the file set
  // always comes from a directory walk; a tsconfig contributes only
  // its compilerOptions (jsx / paths / target).
  const options = buildOptions(target);
  const files = walkFiles(target, _SOURCE_EXTS);
  try {
    return _createProgram(files, options);
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
    return _createProgram(safe, options);
  }
}

function symbolIdFor(sf, node, name) {
  const { line, character } = sf.getLineAndCharacterOfPosition(node.getStart(sf));
  return `ts:${sourceRelative(sf.fileName)}:${name}@${line + 1}:${character + 1}`;
}

function moduleSymbolId(sf) {
  return `ts:${sourceRelative(sf.fileName)}:<module>`;
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
      file: sourceRelative(sf.fileName),
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

function emitVueModuleSymbol(out, sf, compId) {
  writeLine(
    out,
    envelope("symbol", {
      id: moduleSymbolId(sf),
      kind: "module",
      name: "<module>",
      component_id: compId,
      signature: "",
      location: {
        file: sourceRelative(sf.fileName),
        line: 1,
        col: 1,
      },
      visibility: "public",
      is_entry_point: false,
      xml_doc: null,
      metadata: { vue_sfc: true },
      certainty: "asserted",
      created_by: [SOURCE_NAME],
    }),
  );
}

function cmdSymbols(target, outPath) {
  const program = buildProgram(target);
  const out = openOutput(outPath);
  const compId = componentId(target);

  for (const sf of program.getSourceFiles()) {
    if (!_programOwnsSource(program, sf)) continue;
    if (sf.fileName.toLowerCase().endsWith(".vue")) {
      emitVueModuleSymbol(out, sf, compId);
    }

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

function _resolveCalleeId(checker, callExpr, allowedFiles) {
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
      if (!allowedFiles.has(_normKey(declSf.fileName))) continue;
      // A TypeScript symbol can point at any variable declaration, including
      // ``const client = makeClient()``. Resolve only declaration shapes that
      // cmdSymbols actually emits; otherwise ``callee_resolved: true`` would
      // claim a target node that does not exist in the graph.
      const emittedDeclaration =
        decl.kind === ts.SyntaxKind.FunctionDeclaration ||
        decl.kind === ts.SyntaxKind.ClassDeclaration ||
        decl.kind === ts.SyntaxKind.InterfaceDeclaration ||
        decl.kind === ts.SyntaxKind.TypeAliasDeclaration ||
        decl.kind === ts.SyntaxKind.EnumDeclaration ||
        decl.kind === ts.SyntaxKind.MethodDeclaration ||
        decl.kind === ts.SyntaxKind.MethodSignature ||
        (
          decl.kind === ts.SyntaxKind.VariableDeclaration &&
          decl.initializer &&
          (
            decl.initializer.kind === ts.SyntaxKind.ArrowFunction ||
            decl.initializer.kind === ts.SyntaxKind.FunctionExpression ||
            decl.initializer.kind === ts.SyntaxKind.ClassExpression
          )
        );
      if (!emittedDeclaration) continue;
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

function _externalCalleeName(expression) {
  // Keep unresolved identities structural and bounded. ``getText()`` on a
  // chained/minified expression can be an entire inline function or array,
  // which is neither a symbol name nor useful graph evidence.
  if (expression.kind === ts.SyntaxKind.Identifier) return expression.text;
  if (expression.kind === ts.SyntaxKind.ThisKeyword) return "this";
  if (expression.kind === ts.SyntaxKind.SuperKeyword) return "super";
  if (expression.kind !== ts.SyntaxKind.PropertyAccessExpression) return null;

  const parts = [expression.name.text];
  let current = expression.expression;
  while (parts.length < 4) {
    if (current.kind === ts.SyntaxKind.Identifier) {
      parts.unshift(current.text);
      return parts.join(".");
    }
    if (current.kind === ts.SyntaxKind.ThisKeyword) {
      parts.unshift("this");
      return parts.join(".");
    }
    if (current.kind === ts.SyntaxKind.SuperKeyword) {
      parts.unshift("super");
      return parts.join(".");
    }
    if (current.kind !== ts.SyntaxKind.PropertyAccessExpression) break;
    parts.unshift(current.name.text);
    current = current.expression;
  }
  // The receiver is itself a call/array/function expression. The terminal
  // method name (``map``, ``join``, ``then``) is the only stable identity.
  return expression.name.text;
}

function _functionBindingFor(node) {
  if (
    node.kind === ts.SyntaxKind.FunctionDeclaration ||
    node.kind === ts.SyntaxKind.MethodDeclaration
  ) {
    return {
      nodeForId: node,
      name: node.name?.text ?? null,
      isModule: false,
    };
  }
  let parent = node.parent;
  while (parent) {
    if (
      parent.kind === ts.SyntaxKind.VariableDeclaration &&
      parent.name?.kind === ts.SyntaxKind.Identifier
    ) {
      return { nodeForId: parent, name: parent.name.text, isModule: false };
    }
    if (
      parent.kind === ts.SyntaxKind.PropertyAssignment &&
      parent.name &&
      "text" in parent.name
    ) {
      return { nodeForId: parent, name: parent.name.text, isModule: false };
    }
    if (
      parent.kind === ts.SyntaxKind.FunctionDeclaration ||
      parent.kind === ts.SyntaxKind.MethodDeclaration ||
      parent.kind === ts.SyntaxKind.ArrowFunction ||
      parent.kind === ts.SyntaxKind.FunctionExpression
    ) {
      break;
    }
    parent = parent.parent;
  }
  return { nodeForId: node, name: null, isModule: false };
}

function _bindingSymbolId(sf, binding) {
  if (binding?.isModule) return moduleSymbolId(sf);
  if (!binding?.nodeForId || !binding.name) return null;
  return symbolIdFor(sf, binding.nodeForId, binding.name);
}

function emitCallEdges(out, sf, caller, callSite, calleeId, calleeName) {
  const callerId = _bindingSymbolId(sf, caller);
  if (callerId === null) return;
  const start = sf.getLineAndCharacterOfPosition(callSite.getStart(sf));
  // Resolved callee → joinable symbol id, asserted certainty.
  // Unresolved → ``ts:extern:<name>`` so external/built-in calls are
  // distinct from intra-project ones and clearly inferred.
  const resolved = calleeId != null;
  // Bundled/minified code can make ``expression.getText()`` return an entire
  // inline function body. It is not a useful external symbol identity and can
  // exceed the graph contract's 4,096-character identifier ceiling. Filtering
  // it here keeps the producer output authoritative instead of asking ingest
  // to drop malformed records after the analyzer claimed complete coverage.
  if (!resolved && `ts:extern:${calleeName}`.length > 4096) return;
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

// A single TS program over a very large repo (10k+ files) makes the
// type-checker's call resolution (getSymbolAtLocation per call site)
// exhaust memory and the OS kills the process before it emits anything
// (openclaw, 16k ts files → 0 CALLS). Above this file count cmdCalls builds
// one program per directory-grouped chunk so each fits in memory; below it a
// single program keeps full cross-file resolution. Mirrors pack_by_budget.
const _CALLS_CHUNK_THRESHOLD = 4000;
const _CALLS_CHUNK_SIZE = 2500;
const _normKey = (p) => p.replace(/\\/g, "/").toLowerCase();

function chunkFilesByDir(files, size) {
  // Group by directory so calls within a module resolve inside one chunk,
  // then pack groups (sorted, siblings adjacent) into chunks of <= size
  // files. An oversized directory is split, but its files stay contiguous
  // so most intra-directory calls still resolve.
  const byDir = new Map();
  for (const f of files) {
    const d = path.dirname(f);
    if (!byDir.has(d)) byDir.set(d, []);
    byDir.get(d).push(f);
  }
  const chunks = [];
  let cur = [];
  for (const dir of [...byDir.keys()].sort()) {
    for (const f of byDir.get(dir)) {
      cur.push(f);
      if (cur.length >= size) { chunks.push(cur); cur = []; }
    }
  }
  if (cur.length) chunks.push(cur);
  return chunks;
}

function emitCallsForProgram(program, out, onlyFiles) {
  // ``onlyFiles`` (a Set of normalised paths) restricts emission to a
  // chunk's own root files: TS pulls imported files from other chunks into
  // this program too, and walking them would double-emit. null = emit for
  // every source file (the single-program path).
  const checker = program.getTypeChecker();
  const allowedFiles = program.__mnemosAllowedFiles;

  for (const sf of program.getSourceFiles()) {
    if (!_programOwnsSource(program, sf)) continue;
    if (onlyFiles && !onlyFiles.has(_normKey(sf.fileName))) continue;
    // Each entry: { node, nameForId } — name is what emitCallEdges
    // uses to build the caller symbol id. For FunctionDeclaration /
    // MethodDeclaration, ``node.name.text`` exists; for ArrowFunction
    // / FunctionExpression assigned to a const, we look one parent
    // up at the VariableDeclaration. Without this, modern JS code
    // (``const f = () => g()``) silently dropped every callee — a
    // regression PR-111's accuracy harness flagged at recall 0.667.
    const enclosingFn = sf.fileName.toLowerCase().endsWith(".vue")
      ? [{ nodeForId: null, name: "<module>", isModule: true }]
      : [];
    const visit = (node) => {
      const isFn =
        node.kind === ts.SyntaxKind.FunctionDeclaration ||
        node.kind === ts.SyntaxKind.MethodDeclaration ||
        node.kind === ts.SyntaxKind.ArrowFunction ||
        node.kind === ts.SyntaxKind.FunctionExpression;
      if (isFn) {
        enclosingFn.push(_functionBindingFor(node));
      }
      if (node.kind === ts.SyntaxKind.CallExpression) {
        const call = node;
        const caller = enclosingFn[enclosingFn.length - 1];
        if (caller && _bindingSymbolId(sf, caller) !== null) {
          const calleeId = _resolveCalleeId(checker, call, allowedFiles);
          const calleeName = _externalCalleeName(call.expression);
          if (calleeId !== null || calleeName !== null) {
            emitCallEdges(
              out, sf, caller, call, calleeId, calleeName,
            );
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
}

function cmdCalls(target, outPath) {
  const out = openOutput(outPath);
  const files = walkFiles(target, _SOURCE_EXTS);
  if (files.length <= _CALLS_CHUNK_THRESHOLD) {
    // Small/medium repo: one program, full cross-file call resolution.
    emitCallsForProgram(buildProgram(target), out, null);
  } else {
    const chunks = chunkFilesByDir(files, _CALLS_CHUNK_SIZE);
    reportError(
      target,
      `calls: ${files.length} files > ${_CALLS_CHUNK_THRESHOLD}; chunking ` +
        `into ${chunks.length} programs (cross-chunk calls → extern)`,
      true,
    );
    const options = buildOptions(target);
    for (const chunk of chunks) {
      let program;
      try {
        program = _createProgram(chunk, options, files);
      } catch (err) {
        reportError(target, `calls chunk build failed: ${err.message}`, true);
        continue;
      }
      emitCallsForProgram(program, out, new Set(chunk.map(_normKey)));
      // Release this chunk's program before building the next — a TS program
      // over thousands of files holds GBs, and two resident at once OOMs node
      // (the silent OS kill that left openclaw with only chunk 1). ``--expose-gc``
      // (set by the runner) makes the reclaim synchronous so peak memory stays
      // at one chunk, not the whole repo.
      program = undefined;
      if (typeof global !== "undefined" && typeof global.gc === "function") {
        global.gc();
      }
    }
  }
  if (outPath) out.end();
}

// HTTP verbs an Express/Koa-style router exposes as ``router.<verb>()``.
const _HTTP_VERBS = new Set([
  "get", "post", "put", "delete", "patch", "options", "head", "all",
]);
const _HTTP_CLIENT_RECEIVERS = new Set([
  "axios", "httpclient", "apiclient", "restclient", "$http",
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

function _httpReceiverRole(receiver) {
  let name = null;
  if (receiver.kind === ts.SyntaxKind.Identifier) {
    name = receiver.text.toLowerCase();
  } else if (receiver.kind === ts.SyntaxKind.PropertyAccessExpression) {
    name = receiver.name.text.toLowerCase();
  }
  if (name === null) return null;
  if (
    name === "app" ||
    name === "server" ||
    name === "fastify" ||
    name === "router" ||
    name.endsWith("router")
  ) {
    return "server";
  }
  if (_HTTP_CLIENT_RECEIVERS.has(name)) return "client";
  return null;
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
  const callerId = sf.fileName.toLowerCase().endsWith(".vue")
    ? moduleSymbolId(sf)
    : symbolIdFor(sf, node, "<caller>");
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
    if (!_programOwnsSource(program, sf)) continue;

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
          // A literal HTTP method call can be either a server route
          // (``router.post``) or a client request (``httpClient.post``).
          // Classify the receiver explicitly: treating every ``.post("/…")``
          // as Express makes Vue API clients look like endpoint exposers and
          // creates false duplicate-endpoint findings.
          const verb = expr.name.text.toLowerCase();
          const a0 = call.arguments[0];
          const receiverRole = _httpReceiverRole(expr.expression);
          if (
            _HTTP_VERBS.has(verb) &&
            a0?.kind === ts.SyntaxKind.StringLiteral &&
            a0.text.startsWith("/") &&
            receiverRole !== null
          ) {
            emitHttpContract(
              out,
              sf,
              node,
              verb,
              a0.text,
              receiverRole === "server" ? "EXPOSES" : "CALLS",
              receiverRole === "server"
                ? "ts_express_route"
                : "ts_http_client_literal",
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

function emitDataAccess(out, sf, binding, rawEntity, access, site, seen) {
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
  const callerId = binding
    ? _bindingSymbolId(sf, binding)
    : moduleSymbolId(sf);
  if (callerId === null) return;
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
    if (!_programOwnsSource(program, sf)) continue;
    const enclosingFn = [];
    const seen = new Set();
    const visit = (node) => {
      const isFn =
        node.kind === ts.SyntaxKind.FunctionDeclaration ||
        node.kind === ts.SyntaxKind.MethodDeclaration ||
        node.kind === ts.SyntaxKind.ArrowFunction ||
        node.kind === ts.SyntaxKind.FunctionExpression;
      if (isFn) enclosingFn.push(_functionBindingFor(node));
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
  if (target) _analysisRoot = path.resolve(target);

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
