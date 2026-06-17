/* Shared dashboard helpers. Vanilla JS; loaded once from _layout.html so
   every tab can drop the inline alert()/JSON.stringify pattern without
   re-implementing the wheels.

   Highlights:
   * showToast(message, level)      — replaces alert(await r.text()).
   * renderJson(parent, value, opts) — capped recursion depth, collapsible.
   * showError(parent, response)     — friendly rendering for fetch failures.
   * copyToClipboard(text)           — clipboard API with execCommand fallback
                                        so HTTP demos still get a Copy button.
   * showDialog(id), closeDialog(id) — convenience wrappers.
*/

(function () {
  "use strict";

  function ensureToastHost(level) {
    // Two hosts so error / warn toasts can be announced via
    // ``aria-live="assertive"`` while routine info stays polite
    // (Team B 3rd-round should-fix on the UI a11y front).
    var assertive = level === "error" || level === "warn";
    var cls = assertive ? "toast-stack toast-stack-assertive" : "toast-stack";
    var host = document.querySelector("." + (assertive ? "toast-stack-assertive" : "toast-stack-polite"));
    if (!host) {
      host = document.createElement("div");
      host.className = cls + " " + (assertive ? "toast-stack-assertive" : "toast-stack-polite");
      host.setAttribute("role", assertive ? "alert" : "status");
      host.setAttribute("aria-live", assertive ? "assertive" : "polite");
      document.body.appendChild(host);
    }
    return host;
  }

  function showToast(message, level) {
    var host = ensureToastHost(level);
    var el = document.createElement("div");
    el.className = "toast " + (level || "");
    el.textContent = String(message);
    host.appendChild(el);
    var ttl = level === "error" ? 7000 : 4000;
    setTimeout(function () { el.remove(); }, ttl);
  }

  /**
   * Try to copy `text` to the clipboard.
   * Returns true on success; false if both the modern API and the
   * legacy execCommand fallback fail.
   */
  async function copyToClipboard(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (e) { /* fall through */ }
    // execCommand fallback for self-hosted HTTP demos.
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "absolute";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    ta.remove();
    return ok;
  }

  /**
   * Render `value` into `parent` as a collapsible tree. Recursion is
   * capped (default 10) so a malicious or huge payload cannot freeze
   * the browser — branches deeper than the limit print "[depth limit]"
   * and offer a one-click "expand" link that re-renders with depth+5.
   */
  function renderJson(parent, value, opts) {
    var maxDepth = (opts && opts.maxDepth) || 10;
    parent.innerHTML = "";
    parent.appendChild(_renderJsonNode(value, 0, maxDepth));
  }

  function _renderJsonNode(value, depth, maxDepth) {
    if (value === null) return _span("kw", "null");
    if (typeof value === "boolean") return _span("kw", String(value));
    if (typeof value === "number") return _span("num", String(value));
    if (typeof value === "string") return _span("str", '"' + value + '"');

    if (depth >= maxDepth) {
      var truncated = document.createElement("span");
      truncated.className = "trunc";
      truncated.textContent = "[depth limit] ";
      var more = document.createElement("a");
      more.href = "#";
      more.textContent = "expand";
      more.addEventListener("click", function (ev) {
        ev.preventDefault();
        var node = _renderJsonNode(value, 0, 5);
        truncated.replaceWith(node);
      });
      truncated.appendChild(more);
      return truncated;
    }

    if (Array.isArray(value)) {
      var det = document.createElement("details");
      det.open = depth < 2;
      var sum = document.createElement("summary");
      sum.textContent = "Array(" + value.length + ")";
      det.appendChild(sum);
      var ul = document.createElement("ul");
      value.forEach(function (item, i) {
        var li = document.createElement("li");
        var idx = document.createElement("span");
        idx.className = "muted";
        idx.textContent = i + ": ";
        li.appendChild(idx);
        li.appendChild(_renderJsonNode(item, depth + 1, maxDepth));
        ul.appendChild(li);
      });
      det.appendChild(ul);
      return det;
    }

    if (typeof value === "object") {
      var det2 = document.createElement("details");
      det2.open = depth < 2;
      var keys = Object.keys(value);
      var sum2 = document.createElement("summary");
      sum2.textContent = "Object(" + keys.length + ")";
      det2.appendChild(sum2);
      var ul2 = document.createElement("ul");
      keys.forEach(function (k) {
        var li = document.createElement("li");
        var kn = document.createElement("strong");
        kn.textContent = k + ": ";
        li.appendChild(kn);
        li.appendChild(_renderJsonNode(value[k], depth + 1, maxDepth));
        ul2.appendChild(li);
      });
      det2.appendChild(ul2);
      return det2;
    }
    return _span("misc", String(value));
  }

  function _span(klass, text) {
    var s = document.createElement("span");
    s.className = klass;
    s.textContent = text;
    return s;
  }

  /**
   * Surface a fetch() failure in human terms. Tries to parse JSON for a
   * `detail` field (FastAPI convention) and falls back to plain text.
   */
  async function showError(prefix, response) {
    var msg = prefix + ": HTTP " + response.status;
    try {
      var ct = response.headers.get("content-type") || "";
      if (ct.indexOf("application/json") !== -1) {
        var body = await response.json();
        if (body && body.detail) {
          msg += " — " + (typeof body.detail === "string"
            ? body.detail
            : JSON.stringify(body.detail));
        }
      } else {
        var text = await response.text();
        if (text) msg += " — " + text.slice(0, 200);
      }
    } catch (e) { /* keep msg as-is */ }
    showToast(msg, "error");
  }

  function showDialog(id) {
    var d = document.getElementById(id);
    if (d && typeof d.showModal === "function") {
      d.showModal();
    } else if (d) {
      // dialog-polyfill / very old browsers — fall back to `open`.
      d.setAttribute("open", "");
    }
  }
  function closeDialog(id) {
    var d = document.getElementById(id);
    if (d && typeof d.close === "function") d.close();
    else if (d) d.removeAttribute("open");
  }

  /**
   * Cheap HTML escaper. Several tabs duplicated this — promote it so
   * settings.html doesn't crash with `ReferenceError` (the bug the UI
   * audit found while running grep) and so every tab gets the same
   * conservative encoding.
   */
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[<>&"']/g, function (c) {
      return ({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;","'":"&#39;"})[c];
    });
  }

  /**
   * Render JSON from a sibling ``<script type="application/json">``
   * block. Safer than stuffing the value into a ``data-`` attribute
   * (Team B 3rd-round must-fix on XSS): a ``</script>`` sequence in
   * the data is the only thing we have to escape, and even that only
   * if the producer didn't.
   *
   * Usage:
   *   <script type="application/json" id="audit-row-42">{...}</script>
   *   <div id="audit-row-42-render"></div>
   *   ...
   *   MnemosUI.renderJsonFromScript("audit-row-42", "audit-row-42-render");
   */
  function renderJsonFromScript(scriptId, parentId, opts) {
    var script = document.getElementById(scriptId);
    var parent = document.getElementById(parentId);
    if (!script || !parent) return;
    try {
      var value = JSON.parse(script.textContent || "null");
      renderJson(parent, value, opts);
    } catch (e) {
      parent.textContent = "[failed to parse JSON: " + e.message + "]";
    }
  }

  /**
   * Bind a 200-char rationale textarea + counter element. Handles the
   * IME composition edge case Team B 3rd-round critique #6 raised:
   * Safari and some Android keyboards don't fire ``input`` during a
   * compose, so a Korean operator's character count would freeze.
   *
   * Strategy:
   *   * Counter always reflects ``textarea.value.length`` — even during
   *     composition, so the operator sees something move.
   *   * Submit-button enable waits for ``compositionend`` *or* a 300 ms
   *     idle ``input`` event (fallback for browsers that skip
   *     compositionend on blur).
   */
  function bindRationaleCounter(textarea, counter, submitButton, minChars) {
    if (!textarea || !counter) return;
    var composing = false;
    var idleTimer = null;

    function refresh(allowEnable) {
      var len = textarea.value.length;
      counter.textContent = len + " / " + minChars + " chars";
      counter.classList.toggle("ok", len >= minChars);
      textarea.setAttribute("aria-invalid", len < minChars ? "true" : "false");
      if (submitButton) {
        submitButton.disabled = !(allowEnable && len >= minChars);
      }
    }

    textarea.addEventListener("compositionstart", function () { composing = true; refresh(false); });
    textarea.addEventListener("compositionend", function () {
      composing = false;
      refresh(true);
    });
    textarea.addEventListener("input", function () {
      if (composing) {
        refresh(false);
        return;
      }
      // Fallback timer for environments that skip compositionend.
      if (idleTimer) clearTimeout(idleTimer);
      idleTimer = setTimeout(function () { refresh(true); }, 300);
      refresh(!composing);
    });
    refresh(false);
  }

  /**
   * Compute and remember the client/server clock offset from an API
   * response payload that includes ``server_now`` (ISO 8601). Returns
   * a function that maps a server timestamp into the *client* clock.
   *
   * Team B 3rd-round must-fix #4: ``expires_at - issued_at`` is just
   * the TTL. The dialog needs to know remaining time *against the
   * server clock*, even when the user's laptop is several minutes off.
   */
  function clockOffsetFromPayload(payload) {
    var offset = 0;  // server_ms - client_ms (positive if server ahead)
    if (payload && payload.server_now) {
      var serverMs = new Date(payload.server_now).getTime();
      offset = serverMs - Date.now();
    }
    return {
      offset: offset,
      // Convert a server-side ISO timestamp into the equivalent
      // client-clock ms reading so existing setInterval logic works
      // unchanged.
      toClientMs: function (serverIso) {
        return new Date(serverIso).getTime() - offset;
      },
    };
  }

  // ─── Relative time (P2-4) ────────────────────────────────────────────
  // Phase-2 backlog item: every dashboard tab rendered ISO 8601 strings
  // raw, which makes "how long ago was that run?" require mental
  // arithmetic. Markup convention:
  //
  //     <time data-ts="2026-05-12T14:30:00Z">2026-05-12T14:30:00Z</time>
  //
  // ``hydrateRelativeTimes`` walks every such element, replaces the
  // text with the locale's relative phrasing via the standard
  // ``Intl.RelativeTimeFormat`` (no library), and parks the original
  // ISO in ``title=`` so a hover reveals the exact moment.
  //
  // Tabs that build tables dynamically (analysis.html, dashboard.html,
  // findings.html …) should call ``MnemosUI.hydrateRelativeTimes()``
  // after they inject the rows. The initial DOMContentLoaded pass
  // covers any server-rendered ``<time>`` elements.

  function _rtfFor(locale) {
    try {
      return new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
    } catch (_) {
      return new Intl.RelativeTimeFormat("en", { numeric: "auto" });
    }
  }
  var _rtfCache = null;

  function relativeTime(iso, nowMs) {
    // Returns a phrasing like "3 minutes ago" / "in 2 hours". Falls
    // back to the input on parse failure so a malformed timestamp
    // can't blank out a cell.
    if (!iso) return "";
    var t = new Date(iso).getTime();
    if (isNaN(t)) return iso;
    if (!_rtfCache) {
      _rtfCache = _rtfFor(
        (typeof navigator !== "undefined" && navigator.language) || "en",
      );
    }
    var diff = t - (nowMs == null ? Date.now() : nowMs);
    var abs = Math.abs(diff);
    var sec = diff / 1000;
    if (abs < 60 * 1000) return _rtfCache.format(Math.round(sec), "second");
    var min = sec / 60;
    if (abs < 60 * 60 * 1000) return _rtfCache.format(Math.round(min), "minute");
    var hr = min / 60;
    if (abs < 24 * 60 * 60 * 1000) return _rtfCache.format(Math.round(hr), "hour");
    var day = hr / 24;
    if (abs < 30 * 24 * 60 * 60 * 1000) return _rtfCache.format(Math.round(day), "day");
    var mon = day / 30;
    if (abs < 365 * 24 * 60 * 60 * 1000) return _rtfCache.format(Math.round(mon), "month");
    return _rtfCache.format(Math.round(day / 365), "year");
  }

  function hydrateRelativeTimes(root) {
    var scope = root || document;
    var nodes = scope.querySelectorAll("time[data-ts]");
    var now = Date.now();
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var iso = el.getAttribute("data-ts");
      if (!iso) continue;
      el.textContent = relativeTime(iso, now);
      if (!el.title) el.title = iso;
    }
  }

  // Re-hydrate every minute so "3 minutes ago" doesn't go stale.
  if (typeof document !== "undefined") {
    document.addEventListener("DOMContentLoaded", function () {
      hydrateRelativeTimes();
      setInterval(function () { hydrateRelativeTimes(); }, 60 * 1000);
    });
  }

  // ─── CSRF (PR-44, audit E1) ───────────────────────────────────────
  //
  // The CSRF middleware sets a ``mnemos_csrf`` cookie on every
  // dashboard render. State-changing fetches must echo the value
  // back in an ``X-CSRF-Token`` header. We patch ``window.fetch``
  // here so every existing call site (and every future one) gets
  // the header without per-call boilerplate.
  //
  // GET / HEAD / OPTIONS skip the header — they don't need it and
  // adding it would force an OPTIONS preflight unnecessarily.

  function _readCookie(name) {
    var parts = (document.cookie || "").split(";");
    for (var i = 0; i < parts.length; i++) {
      var trimmed = parts[i].trim();
      if (trimmed.indexOf(name + "=") === 0) {
        return trimmed.slice(name.length + 1);
      }
    }
    return "";
  }

  (function _patchFetch() {
    if (typeof window === "undefined" || !window.fetch) return;
    var orig = window.fetch.bind(window);
    window.fetch = function (input, init) {
      init = init || {};
      var method = (init.method || (typeof input === "string" ? "GET" : (input.method || "GET")))
        .toString().toUpperCase();
      // CSRF header for state-changing requests.
      if (method !== "GET" && method !== "HEAD" && method !== "OPTIONS") {
        var token = _readCookie("mnemos_csrf");
        if (token) {
          init.headers = new Headers(init.headers || {});
          if (!init.headers.has("X-CSRF-Token")) {
            init.headers.set("X-CSRF-Token", token);
          }
        }
      }
      // PR-47 — auto-redirect on 401 so an operator whose session
      // idled-out doesn't see a generic "HTTP 401" error and have
      // to guess they need to sign in again. The /login and /api
      // probes are skipped so the login page itself doesn't redirect
      // to itself in a loop.
      return orig(input, init).then(function (r) {
        if (r.status === 401 && typeof window !== "undefined") {
          var path = window.location.pathname;
          var onAuthFlow = path === "/login" || path === "/forgot"
            || path === "/reset" || path === "/invite";
          if (!onAuthFlow) {
            // One-shot toast so the operator knows what happened
            // before the page disappears under them.
            if (typeof showToast === "function") {
              try {
                showToast(
                  typeof t === "function"
                    ? t("Session expired. Redirecting to sign in…")
                    : "Session expired. Redirecting to sign in…",
                  "warn",
                );
              } catch (_) {}
            }
            // Preserve the current path so a future PR can deep-
            // link back after re-auth.
            try { sessionStorage.setItem("mnemos_post_login_path", path); } catch (_) {}
            setTimeout(function () { window.location.href = "/login"; }, 600);
          }
        }
        return r;
      });
    };
  })();

  // ─── CSV export (PR-47, audit F3) ─────────────────────────────────
  //
  // ``MnemosUI.exportCsv(rows, columns, filename)`` builds a CSV
  // file from a JS array of records and triggers a download.
  // Pure client-side; works on any array of plain objects.
  //
  // Each value is run through ``_csvCell`` which handles the four
  // standard CSV escape cases (comma, quote, newline, leading
  // sign/equals as a defence against CSV-injection in Excel).

  function _csvCell(value) {
    if (value === null || value === undefined) return "";
    var s = typeof value === "object" ? JSON.stringify(value) : String(value);
    // CSV-injection defence — Excel runs `= + - @` as formulas.
    if (s.length && "=+-@".indexOf(s.charAt(0)) !== -1) {
      s = "'" + s;
    }
    if (s.indexOf(",") !== -1 || s.indexOf('"') !== -1 || s.indexOf("\n") !== -1) {
      s = '"' + s.replace(/"/g, '""') + '"';
    }
    return s;
  }

  function exportCsv(rows, columns, filename) {
    columns = columns || (rows.length ? Object.keys(rows[0]) : []);
    var header = columns.map(_csvCell).join(",");
    var body = rows.map(function (r) {
      return columns.map(function (c) { return _csvCell(r[c]); }).join(",");
    }).join("\n");
    var csv = header + "\n" + body;
    var blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    _downloadBlob(blob, filename || ("mnemos-export-" + Date.now() + ".csv"));
  }

  // Shared blob → download helper (used by both CSV and XLSX paths).
  function _downloadBlob(blob, filename) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  // ─── XLSX export (ExcelJS — PR-134) ──────────────────────────────
  //
  // exceljs.github.io / github.com/exceljs/exceljs (MIT). The library
  // is ~950 KB minified, so it is **lazy-loaded** on first export
  // rather than shipped on every page — a dashboard tab that never
  // exports Excel pays zero bytes for it. The script is served from
  // ``/static/exceljs.min.js`` so CSP ``script-src 'self'`` covers it
  // (no CDN, works air-gapped).
  //
  // ``MnemosUI.exportXlsx(spec, filename)`` accepts either shape:
  //   1. an array of plain records  → a single "Sheet1"
  //   2. an array of {name, rows, columns?} → one worksheet each
  // Styled header (bold + fill), frozen header row, autofilter, and
  // the same CSV-injection guard ExcelJS needs for ``= + - @`` cells.

  var _exceljsPromise = null;

  function _loadExcelJS() {
    if (window.ExcelJS) return Promise.resolve(window.ExcelJS);
    if (_exceljsPromise) return _exceljsPromise;
    _exceljsPromise = new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = "/static/exceljs.min.js";
      s.async = true;
      s.onload = function () {
        if (window.ExcelJS) resolve(window.ExcelJS);
        else reject(new Error("ExcelJS loaded but window.ExcelJS missing"));
      };
      s.onerror = function () {
        _exceljsPromise = null;  // allow a retry on next click
        reject(new Error("failed to load /static/exceljs.min.js"));
      };
      document.head.appendChild(s);
    });
    return _exceljsPromise;
  }

  // Same formula-injection defence as _csvCell — Excel/Sheets execute
  // a cell whose text begins with = + - @. We prefix a single quote so
  // the value renders literally instead of evaluating.
  function _xlsxSafe(value) {
    if (value === null || value === undefined) return "";
    var s = typeof value === "object" ? JSON.stringify(value) : value;
    if (typeof s === "string" && s.length && "=+-@".indexOf(s.charAt(0)) !== -1) {
      return "'" + s;
    }
    return s;
  }

  function _normaliseSheets(spec) {
    // Array of plain records → single sheet. Array of sheet specs →
    // as-is. A sheet spec is {name, rows, columns?}.
    if (!Array.isArray(spec)) return [];
    var looksLikeSheetSpecs = spec.length > 0 &&
      spec[0] && typeof spec[0] === "object" &&
      Array.isArray(spec[0].rows);
    if (looksLikeSheetSpecs) return spec;
    return [{ name: "Sheet1", rows: spec }];
  }

  function exportXlsx(spec, filename, opts) {
    opts = opts || {};
    var sheets = _normaliseSheets(spec);
    return _loadExcelJS().then(function (ExcelJS) {
      var wb = new ExcelJS.Workbook();
      wb.creator = "Mnemos";
      wb.created = new Date();
      sheets.forEach(function (sheet, idx) {
        var rows = sheet.rows || [];
        var columns = sheet.columns ||
          (rows.length ? Object.keys(rows[0]) : []);
        var ws = wb.addWorksheet(sheet.name || ("Sheet" + (idx + 1)));
        // Column definitions drive header text + a reasonable width.
        ws.columns = columns.map(function (c) {
          var header = typeof c === "object" ? (c.header || c.key) : c;
          var key = typeof c === "object" ? c.key : c;
          var width = typeof c === "object" && c.width ? c.width
            : Math.min(60, Math.max(12, String(header).length + 4));
          return { header: String(header), key: String(key), width: width };
        });
        var keys = ws.columns.map(function (col) { return col.key; });
        rows.forEach(function (r) {
          var rowObj = {};
          keys.forEach(function (k) { rowObj[k] = _xlsxSafe(r[k]); });
          ws.addRow(rowObj);
        });
        // Header styling — bold white on slate, frozen, autofiltered.
        var headerRow = ws.getRow(1);
        headerRow.font = { bold: true, color: { argb: "FFFFFFFF" } };
        headerRow.fill = {
          type: "pattern", pattern: "solid",
          fgColor: { argb: "FF1F2933" },
        };
        headerRow.alignment = { vertical: "middle" };
        ws.views = [{ state: "frozen", ySplit: 1 }];
        if (keys.length) {
          ws.autoFilter = {
            from: { row: 1, column: 1 },
            to: { row: 1, column: keys.length },
          };
        }
      });
      return wb.xlsx.writeBuffer();
    }).then(function (buffer) {
      var blob = new Blob([buffer], {
        type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      });
      _downloadBlob(blob, filename || ("mnemos-export-" + Date.now() + ".xlsx"));
    }).catch(function (err) {
      // Surface a toast rather than a silent failure — the operator
      // clicked a button and deserves to know it didn't work.
      if (typeof showToast === "function") {
        showToast(
          t("Excel export failed — falling back to CSV is available."),
          "error"
        );
      }
      throw err;
    });
  }

  // ─── Project picker (PR-137 — UX friction kill) ────────────────
  //
  // ``MnemosUI.mountProjectPicker(host, opts?)`` upgrades a UUID
  // ``<input>`` into a ``<select>`` populated from ``/api/v1/projects``,
  // pre-filled from ``?project=<id>``, and optionally auto-submitting
  // its parent form. Pre-fix every dashboard tab demanded that the
  // operator copy a 36-char UUID by hand — the #1 friction the audit
  // surfaced.
  //
  // - ``host``: an ``<input>`` (replaced) or ``<select>`` (filled).
  //   The picker keeps the host's ``name`` / ``id`` / ``required`` so
  //   form submission stays identical for downstream code.
  // - ``opts.autoSubmit`` (bool, default false): if a ``?project=…``
  //   param was honoured, dispatch a ``submit`` event on the parent
  //   form so the operator lands on a pre-loaded view, not a blank one.
  // - ``opts.placeholder`` (str): first option label when no project
  //   is preselected (default: localised "Select a project").
  //
  // The fetch is cached for the page lifetime — multiple pickers on
  // one screen share one API round-trip.

  var _projectsPromise = null;

  function _fetchProjectsOnce() {
    if (_projectsPromise) return _projectsPromise;
    _projectsPromise = fetch("/api/v1/projects", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : []; })
      .catch(function () { return []; });
    return _projectsPromise;
  }

  function currentProjectFromUrl() {
    try {
      var u = new URLSearchParams(window.location.search);
      return u.get("project") || "";
    } catch (_) { return ""; }
  }

  function mountProjectPicker(host, opts) {
    opts = opts || {};
    if (typeof host === "string") host = document.querySelector(host);
    if (!host) return Promise.resolve(null);
    // Preset from the URL ``?project=`` OR the globally-selected project
    // (mnemos_last_project) so picking a project once carries across every
    // page without re-selecting on each screen (PR-173 global selection).
    var preset = _currentProjectContext();
    var name = host.getAttribute("name") || "project_id";
    var hostId = host.id || "project-picker";
    var required = host.hasAttribute("required");
    var formEl = host.closest("form");

    return _fetchProjectsOnce().then(function (projects) {
      var sel = document.createElement("select");
      sel.id = hostId;
      sel.setAttribute("name", name);
      if (required) sel.setAttribute("required", "");
      sel.className = "mnemos-project-picker";
      var ph = document.createElement("option");
      ph.value = "";
      ph.textContent = opts.placeholder || t("Select a project");
      sel.appendChild(ph);
      var matched = false;
      (projects || []).forEach(function (p) {
        var o = document.createElement("option");
        o.value = p.id;
        o.textContent = p.name + " — " + (p.id || "").slice(0, 8);
        if (p.id === preset) { o.selected = true; matched = true; }
        sel.appendChild(o);
      });
      // Operator landed on /findings?project=<id> for a project we
      // can't see — surface that without silently dropping the value.
      if (preset && !matched) {
        var o = document.createElement("option");
        o.value = preset;
        o.textContent = t("Unknown project") + " — " + preset.slice(0, 8);
        o.selected = true;
        sel.appendChild(o);
      }
      // Replace the input in-place so existing CSS classes /
      // <label> ``for=`` references survive.
      if (host.parentNode) host.parentNode.replaceChild(sel, host);

      if (preset && formEl && opts.autoSubmit !== false) {
        // Auto-load whenever a project is preset (from the URL or the
        // global selection) so the operator picks a project once and
        // every page shows its data without a second click (PR-173).
        // Pages whose form needs more than a project (e.g. Ask needs a
        // question) pass ``autoSubmit: false`` to opt out.
        // Defer one tick so caller's load() handler is registered
        // before we trigger it.
        setTimeout(function () {
          try {
            if (typeof formEl.requestSubmit === "function") {
              formEl.requestSubmit();
            } else {
              formEl.dispatchEvent(new Event("submit", {
                bubbles: true, cancelable: true,
              }));
            }
          } catch (_) { /* ignore */ }
        }, 0);
      }
      return sel;
    });
  }

  // ─── Mermaid renderer (PR-136 — graphs/sequence/charts) ──────────
  //
  // ``MnemosUI.renderMermaid(host, code, opts?)`` renders Mermaid
  // syntax (flowchart, sequence, gantt, pie, state, ER, journey, …)
  // into ``host``. The library (~3.2 MB minified) is **lazy-loaded**
  // on first call so tabs that never render a diagram pay zero bytes
  // for it — same pattern as ``_loadExcelJS``. Served from
  // ``/static/mermaid.min.js`` (self-hosted, MIT) so the locked-down
  // CSP from PR-130 never has to allow a third-party origin.

  var _mermaidPromise = null;
  var _mermaidSeq = 0;

  function _loadMermaid() {
    if (window.mermaid) return Promise.resolve(window.mermaid);
    if (_mermaidPromise) return _mermaidPromise;
    _mermaidPromise = new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = "/static/mermaid.min.js";
      s.async = true;
      s.onload = function () {
        if (window.mermaid) {
          var theme = document.documentElement
            .getAttribute("data-theme") === "dark" ? "dark" : "default";
          window.mermaid.initialize({
            startOnLoad: false,
            securityLevel: "strict",  // sanitises labels — no inline HTML
            theme: theme,
            fontFamily: "var(--font-mono, monospace)",
          });
          resolve(window.mermaid);
        } else {
          reject(new Error("mermaid loaded but window.mermaid missing"));
        }
      };
      s.onerror = function () {
        _mermaidPromise = null;  // allow a retry on next call
        reject(new Error("failed to load /static/mermaid.min.js"));
      };
      document.head.appendChild(s);
    });
    return _mermaidPromise;
  }

  function renderMermaid(host, code, opts) {
    opts = opts || {};
    if (typeof host === "string") host = document.querySelector(host);
    if (!host) return Promise.reject(new Error("renderMermaid: host missing"));
    return _loadMermaid().then(function (mermaid) {
      var id = "mermaid-" + (++_mermaidSeq);
      return mermaid.render(id, String(code || "")).then(function (out) {
        host.innerHTML = out.svg;
        if (typeof out.bindFunctions === "function") {
          out.bindFunctions(host);
        }
        return host;
      });
    }).catch(function (err) {
      host.innerHTML = '<p class="muted" role="alert">'
        + escapeHtml(t("Diagram render failed: ") + (err && err.message || err))
        + "</p>";
      throw err;
    });
  }

  // ─── Command palette (PR-42, audit C2 + D3) ──────────────────────
  //
  // ``cmd/ctrl+K`` (or ``/``) anywhere on the dashboard opens a
  // search-and-navigate overlay. Phase 1 entries are:
  //
  //   * static nav targets — Dashboard, Projects, Analysis, … with
  //     keyboard shortcuts (``g d``, ``g p``, …) à la GitHub;
  //   * project list — typing a name jumps to /analysis?project=<id>;
  //   * findings — typing a kind / subject id from the cached set.
  //
  // The palette runs entirely in the browser; the GET-/api/v1/projects
  // call is cached on first open so each subsequent ``cmd+K`` is
  // instant. Server-side global search lives in a future PR.

  var _PALETTE_PROJECTS = null;  // cached on first open

  function _paletteEl() { return document.getElementById("cmdk-overlay"); }

  function _buildPaletteEntries(projects) {
    var nav = [
      { label: "Dashboard",   href: "/",            shortcut: "g d", icon: "" },
      { label: "Projects",    href: "/projects",    shortcut: "g p", icon: "" },
      { label: "Analysis",    href: "/analysis",    shortcut: "g a", icon: "" },
      { label: "Data",        href: "/data",        shortcut: "g t", icon: "" },
      { label: "Plans",       href: "/plans",       shortcut: "",     icon: "" },
      { label: "Diffs",       href: "/diffs",       shortcut: "g r", icon: "" },
      { label: "Findings",    href: "/findings",    shortcut: "g f", icon: "" },
      { label: "Audit",       href: "/audit",       shortcut: "",     icon: "" },
      { label: "Settings",    href: "/settings",    shortcut: "g s", icon: "" },
      { label: "Health",      href: "/health",      shortcut: "g h", icon: "" },
      { label: "Docs",        href: "/docs",        shortcut: "g k", icon: "" },
      { label: "Profile",     href: "/profile",     shortcut: "",     icon: "" },
    ];
    var entries = nav.map(function (n) { return Object.assign({ kind: "nav" }, n); });
    (projects || []).forEach(function (p) {
      entries.push({
        kind: "project",
        label: p.name,
        sublabel: p.id,
        href: "/analysis?project=" + encodeURIComponent(p.id),
      });
    });
    return entries;
  }

  function _filterPaletteEntries(entries, q) {
    if (!q) return entries;
    var qLower = q.toLowerCase();
    return entries.filter(function (e) {
      return (
        e.label.toLowerCase().indexOf(qLower) !== -1 ||
        (e.sublabel && String(e.sublabel).toLowerCase().indexOf(qLower) !== -1)
      );
    });
  }

  function _renderPaletteResults(entries, selected) {
    var list = document.getElementById("cmdk-list");
    if (!list) return;
    if (!entries.length) {
      list.innerHTML = '<li class="cmdk-empty">' +
        (typeof t === "function" ? t("No matches.") : "No matches.") +
        '</li>';
      return;
    }
    list.innerHTML = entries.slice(0, 20).map(function (e, i) {
      var sub = e.sublabel ? '<span class="cmdk-sub muted">' + escapeHtml(String(e.sublabel)) + '</span>' : "";
      var sc = e.shortcut ? '<kbd class="cmdk-sc">' + escapeHtml(e.shortcut) + '</kbd>' : "";
      // PR-54 — kind badge for project / node / finding search hits.
      var kindBadge = "";
      if (e.kind === "project" || e.kind === "node" || e.kind === "finding") {
        kindBadge = '<span class="cmdk-kind cmdk-kind-' + e.kind + '">'
          + escapeHtml(e.kind) + '</span>';
      }
      return '<li class="cmdk-item ' + (i === selected ? "active" : "") + '" data-href="' + escapeHtml(e.href) + '">' +
        kindBadge +
        '<span class="cmdk-label">' + escapeHtml(e.label) + '</span>' +
        sub + sc +
      "</li>";
    }).join("");
  }

  async function _loadPaletteProjects() {
    if (_PALETTE_PROJECTS !== null) return _PALETTE_PROJECTS;
    try {
      var r = await fetch("/api/v1/projects");
      _PALETTE_PROJECTS = r.ok ? await r.json() : [];
    } catch (_) {
      _PALETTE_PROJECTS = [];
    }
    return _PALETTE_PROJECTS;
  }

  async function _openPalette() {
    var overlay = _paletteEl();
    if (!overlay) return;
    overlay.classList.add("open");
    var input = document.getElementById("cmdk-input");
    if (input) {
      input.value = "";
      setTimeout(function () { input.focus(); }, 10);
    }
    var projects = await _loadPaletteProjects();
    var entries = _buildPaletteEntries(projects);
    overlay._entries = entries;
    overlay._selected = 0;
    _renderPaletteResults(entries, 0);
  }

  function _closePalette() {
    var overlay = _paletteEl();
    if (overlay) overlay.classList.remove("open");
  }

  // PR-54 — global search. When the operator's query is ≥ 3 chars
  // and a project context is known (last project they visited, or
  // a ``?project=`` in the URL), the palette also queries the
  // graph-node search API and the findings list, so a search for
  // "payment" surfaces graph symbols + findings, not just nav
  // shortcuts. Debounced 250ms so each keystroke doesn't fire a
  // fetch.
  var _paletteSearchTimer = null;

  function _currentProjectContext() {
    var fromUrl = new URLSearchParams(window.location.search).get("project");
    if (fromUrl) return fromUrl;
    try {
      return localStorage.getItem("mnemos_last_project") || "";
    } catch (_) {
      return "";
    }
  }

  // Pages call this when the operator picks a project, so the
  // command palette's global search has a context to query.
  function rememberProject(projectId) {
    if (!projectId) return;
    try { localStorage.setItem("mnemos_last_project", projectId); } catch (_) {}
  }

  async function _remoteSearch(query) {
    var pid = _currentProjectContext();
    if (!pid || query.length < 3) return [];
    var entries = [];
    try {
      var gr = await fetch(
        "/api/v1/projects/" + encodeURIComponent(pid)
        + "/graph/search?limit=8&q=" + encodeURIComponent(query)
      );
      if (gr.ok) {
        var nodes = await gr.json();
        nodes.forEach(function (n) {
          entries.push({
            kind: "node",
            label: n.id,
            sublabel: n.kind,
            href: "/graph?project=" + encodeURIComponent(pid),
          });
        });
      }
    } catch (_) {}
    try {
      var fr = await fetch(
        "/api/v1/projects/" + encodeURIComponent(pid) + "/findings?limit=8"
      );
      if (fr.ok) {
        var findings = await fr.json();
        var qLower = query.toLowerCase();
        findings
          .filter(function (f) {
            return (f.kind || "").toLowerCase().indexOf(qLower) !== -1
              || (f.subject_node_id || "").toLowerCase().indexOf(qLower) !== -1;
          })
          .forEach(function (f) {
            entries.push({
              kind: "finding",
              label: f.kind + " · " + (f.priority || ""),
              sublabel: f.subject_node_id || "",
              href: "/findings?project=" + encodeURIComponent(pid),
            });
          });
      }
    } catch (_) {}
    return entries;
  }

  function _onPaletteInput(ev) {
    var overlay = _paletteEl();
    if (!overlay || !overlay._entries) return;
    var query = ev.target.value;
    var filtered = _filterPaletteEntries(overlay._entries, query);
    overlay._filtered = filtered;
    overlay._selected = 0;
    _renderPaletteResults(filtered, 0);
    // Debounced remote search merged in on top of the static
    // (nav + project) results.
    if (_paletteSearchTimer) clearTimeout(_paletteSearchTimer);
    if (query.length >= 3) {
      _paletteSearchTimer = setTimeout(function () {
        _remoteSearch(query).then(function (remote) {
          // Guard: the operator may have typed more since the
          // fetch started — only merge if the box still matches.
          var input = document.getElementById("cmdk-input");
          if (!input || input.value !== query) return;
          var merged = _filterPaletteEntries(overlay._entries, query)
            .concat(remote);
          overlay._filtered = merged;
          overlay._selected = 0;
          _renderPaletteResults(merged, 0);
        });
      }, 250);
    }
  }

  function _onPaletteKey(ev) {
    var overlay = _paletteEl();
    if (!overlay || !overlay.classList.contains("open")) return;
    var entries = overlay._filtered || overlay._entries || [];
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      overlay._selected = Math.min((overlay._selected || 0) + 1, entries.length - 1);
      _renderPaletteResults(entries, overlay._selected);
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      overlay._selected = Math.max((overlay._selected || 0) - 1, 0);
      _renderPaletteResults(entries, overlay._selected);
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      var pick = entries[overlay._selected || 0];
      if (pick) {
        _closePalette();
        window.location.href = pick.href;
      }
    } else if (ev.key === "Escape") {
      _closePalette();
    }
  }

  // Global keybindings.
  if (typeof document !== "undefined") {
    document.addEventListener("DOMContentLoaded", function () {
      // cmd/ctrl+K or ``/`` opens the palette. ``/`` doesn't fire when
      // the focus is in an input/textarea so a search query in a
      // page-level filter still types ``/``.
      document.addEventListener("keydown", function (ev) {
        var inField = /^(INPUT|TEXTAREA|SELECT)$/i.test(
          (ev.target && ev.target.tagName) || ""
        );
        if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "k") {
          ev.preventDefault();
          _openPalette();
          return;
        }
        if (!inField && ev.key === "/") {
          ev.preventDefault();
          _openPalette();
          return;
        }
        // GitHub-style ``g <letter>`` shortcuts.
        if (!inField && !ev.metaKey && !ev.ctrlKey && !ev.altKey) {
          if (ev.key === "g") {
            _gPending = Date.now();
            return;
          }
          if (_gPending && Date.now() - _gPending < 1500) {
            var dest = _G_MAP[ev.key];
            _gPending = 0;
            if (dest) {
              ev.preventDefault();
              window.location.href = dest;
              return;
            }
          }
        }
      });

      // Palette input wiring.
      var input = document.getElementById("cmdk-input");
      if (input) {
        input.addEventListener("input", _onPaletteInput);
      }
      document.addEventListener("keydown", _onPaletteKey);
      // Click handlers on the overlay.
      var overlay = _paletteEl();
      if (overlay) {
        overlay.addEventListener("click", function (ev) {
          // Click on the backdrop closes; click on an item navigates.
          var item = ev.target && ev.target.closest && ev.target.closest(".cmdk-item");
          if (item) {
            _closePalette();
            window.location.href = item.getAttribute("data-href");
          } else if (ev.target === overlay) {
            _closePalette();
          }
        });
      }
    });
  }

  var _gPending = 0;
  var _G_MAP = {
    "d": "/", "p": "/projects", "a": "/analysis", "t": "/data",
    "r": "/diffs", "f": "/findings", "s": "/settings", "h": "/health",
    "k": "/docs",
  };

  // ─── Icons (PR-41) ────────────────────────────────────────────────
  //
  // A small inline-SVG icon set drawn from the Heroicons (MIT) shape
  // catalogue. Each entry is the inner ``<path>`` markup — the SVG
  // wrapper is added by ``icon()`` so callers can size + colour
  // them with CSS (``currentColor`` flows naturally).
  //
  // Usage:
  //     element.innerHTML = MnemosUI.icon("bell");
  //     element.innerHTML = MnemosUI.icon("check", { size: 20 });
  //
  // The set is intentionally tiny — only the icons the dashboard
  // actually uses today. Adding a new one is one entry in
  // ``_ICONS`` and one ``MnemosUI.icon("name")`` call.

  var _ICONS = {
    bell: '<path stroke-linecap="round" stroke-linejoin="round" d="M14.857 17.082a23.848 23.848 0 005.454-1.31A8.967 8.967 0 0118 9.75v-.7V9A6 6 0 006 9v.75a8.967 8.967 0 01-2.312 6.022c1.733.64 3.56 1.085 5.455 1.31m5.714 0a24.255 24.255 0 01-5.714 0m5.714 0a3 3 0 11-5.714 0"/>',
    check: '<path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/>',
    x: '<path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/>',
    warn: '<path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"/>',
    info: '<path stroke-linecap="round" stroke-linejoin="round" d="M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z"/>',
    search: '<path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z"/>',
    plus: '<path stroke-linecap="round" stroke-linejoin="round" d="M12 4.5v15m7.5-7.5h-15"/>',
    cog: '<path stroke-linecap="round" stroke-linejoin="round" d="M10.343 3.94c.09-.542.56-.94 1.11-.94h1.093c.55 0 1.02.398 1.11.94l.149.894c.07.424.384.764.78.93.398.164.855.142 1.205-.108l.737-.527a1.125 1.125 0 011.45.12l.773.774c.39.389.44 1.002.12 1.45l-.527.737c-.25.35-.272.806-.107 1.204.165.397.505.71.93.78l.893.15c.543.09.94.56.94 1.109v1.094c0 .55-.397 1.02-.94 1.11l-.894.149c-.424.07-.764.383-.929.78-.165.398-.143.854.107 1.204l.527.738c.32.447.269 1.06-.12 1.45l-.774.773a1.125 1.125 0 01-1.449.12l-.738-.527c-.35-.25-.806-.272-1.203-.107-.398.165-.71.505-.781.929l-.149.894c-.09.542-.56.94-1.11.94h-1.094c-.55 0-1.019-.398-1.11-.94l-.148-.894c-.071-.424-.384-.764-.781-.93-.398-.164-.854-.142-1.204.108l-.738.527c-.447.32-1.06.269-1.45-.12l-.773-.774a1.125 1.125 0 01-.12-1.45l.527-.737c.25-.35.273-.806.108-1.204-.165-.397-.506-.71-.93-.78l-.894-.15c-.542-.09-.94-.56-.94-1.109v-1.094c0-.55.398-1.02.94-1.11l.894-.149c.424-.07.765-.383.93-.78.165-.398.143-.854-.108-1.204l-.526-.738a1.125 1.125 0 01.12-1.45l.773-.773a1.125 1.125 0 011.45-.12l.737.527c.35.25.807.272 1.204.107.397-.165.71-.505.78-.929l.15-.894z"/><path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>',
  };

  function icon(name, opts) {
    var size = (opts && opts.size) || 18;
    var path = _ICONS[name];
    if (!path) return "";
    var classAttr = (opts && opts.cls) ? ' class="' + opts.cls + '"' : "";
    return (
      '<svg xmlns="http://www.w3.org/2000/svg" width="' + size + '" height="' + size +
      '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"' +
      classAttr + ' aria-hidden="true">' + path + "</svg>"
    );
  }

  // ─── Comment thread mount (PR-43) ─────────────────────────────────
  //
  // ``MnemosUI.mountCommentThread(container, kind, id)`` renders a
  // comment thread inside ``container`` (a DOM element) for the
  // given target (``kind`` ∈ {plan, diff_submission}, ``id`` is the
  // target UUID). Handles list + create + edit + delete via the
  // ``/api/v1/comments`` endpoints.

  async function _loadComments(kind, targetId) {
    var qs = new URLSearchParams({ target_kind: kind, target_id: targetId });
    var r = await fetch("/api/v1/comments?" + qs.toString());
    return r.ok ? r.json() : [];
  }

  async function _postComment(kind, targetId, body) {
    var qs = new URLSearchParams({ target_kind: kind, target_id: targetId });
    var r = await fetch("/api/v1/comments?" + qs.toString(), {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ body: body }),
    });
    if (!r.ok) {
      await showError("Comment failed", r);
      return null;
    }
    return r.json();
  }

  function _renderCommentList(target, comments, kind, targetId, currentUserId, currentUserRole) {
    if (!comments.length) {
      target.innerHTML = '<p class="muted">' +
        (typeof t === "function" ? t("No comments yet.") : "No comments yet.") +
        '</p>';
      return;
    }
    target.innerHTML = comments.map(function (c) {
      var when = c.created_at
        ? '<time data-ts="' + escapeHtml(c.created_at) + '">' + escapeHtml(c.created_at) + '</time>'
        : "";
      var edited = c.edited_at ? ' <span class="muted">(edited)</span>' : "";
      var author = c.author_display_name || c.author_username || "—";
      var ownerActions = (c.author_id === currentUserId || currentUserRole === "admin")
        ? '<div class="comment-actions">' +
            '<button onclick="_mnemosEditComment(\'' + c.id + '\', \'' + kind + '\', \'' + targetId + '\')">Edit</button>' +
            '<button onclick="_mnemosDeleteComment(\'' + c.id + '\', \'' + kind + '\', \'' + targetId + '\')">Delete</button>' +
          '</div>'
        : "";
      return (
        '<div class="comment" data-comment-id="' + c.id + '">' +
          '<div class="comment-meta">' +
            '<span class="comment-author">' + escapeHtml(author) + '</span>' +
            '<span>' + when + edited + '</span>' +
          '</div>' +
          '<div class="comment-body">' + escapeHtml(c.body) + '</div>' +
          ownerActions +
        '</div>'
      );
    }).join("");
    hydrateRelativeTimes(target);
  }

  async function mountCommentThread(container, kind, targetId, opts) {
    opts = opts || {};
    container.innerHTML =
      '<div class="comment-thread">' +
        '<h3>' + (typeof t === "function" ? t("Comments") : "Comments") + '</h3>' +
        '<div data-role="comment-list"><div class="skeleton skeleton-card"></div></div>' +
        '<form class="comment-form" onsubmit="return _mnemosSubmitComment(event, \'' + kind + '\', \'' + targetId + '\')">' +
          '<textarea required maxlength="4000" placeholder="' +
            escapeHtml(typeof t === "function" ? t("Write a comment…") : "Write a comment…") +
          '"></textarea>' +
          '<button type="submit" class="primary">' +
            (typeof t === "function" ? t("Post comment") : "Post comment") +
          '</button>' +
        '</form>' +
      '</div>';

    // Load current user for ownership-based action visibility.
    var me = null;
    try {
      var meRes = await fetch("/api/v1/auth/me");
      if (meRes.ok) me = await meRes.json();
    } catch (_) {}

    var listEl = container.querySelector('[data-role="comment-list"]');
    var comments = await _loadComments(kind, targetId);
    _renderCommentList(
      listEl, comments, kind, targetId,
      me ? me.id : null, me ? me.role : null,
    );
    // Stash the reload function so the form submit handler can call it.
    container._mnemosReload = async function () {
      var fresh = await _loadComments(kind, targetId);
      _renderCommentList(
        listEl, fresh, kind, targetId,
        me ? me.id : null, me ? me.role : null,
      );
    };
  }

  // Form submit handler — global so the inline onsubmit attribute
  // can reach it. Defensive: looks up the container at submit time.
  window._mnemosSubmitComment = async function (ev, kind, targetId) {
    ev.preventDefault();
    var ta = ev.target.querySelector("textarea");
    if (!ta || !ta.value.trim()) return false;
    var created = await _postComment(kind, targetId, ta.value);
    if (created) {
      ta.value = "";
      var container = ev.target.closest("[data-comment-thread]");
      if (container && container._mnemosReload) await container._mnemosReload();
    }
    return false;
  };

  // PR-138e — replaced the bare ``prompt("Edit comment:")`` with a
  // proper Promise-based modal. ``window.prompt`` is unstyled, has
  // no validation, and breaks dark theme — the UX audit's "drops to
  // circa-2000" complaint. The modal preserves Cancel/Save semantics
  // (returns null on Cancel) and wires Esc/Enter for keyboard ops.
  function _editCommentDialog(currentBody) {
    return new Promise(function (resolve) {
      var existing = document.getElementById("mnemos-edit-comment-dialog");
      if (existing) existing.remove();
      var dlg = document.createElement("dialog");
      dlg.id = "mnemos-edit-comment-dialog";
      dlg.className = "modal";
      dlg.setAttribute("aria-labelledby", "mnemos-edit-comment-title");
      dlg.innerHTML =
        '<header><h2 id="mnemos-edit-comment-title">'
        + escapeHtml(t("Edit comment")) + '</h2></header>'
        + '<div class="body">'
        + '<textarea id="mnemos-edit-comment-ta" '
        + 'aria-label="' + escapeHtml(t("Comment body")) + '"></textarea>'
        + '</div>'
        + '<menu>'
        + '<button type="button" id="mnemos-edit-comment-cancel">'
        + escapeHtml(t("Cancel")) + '</button>'
        + '<button type="button" id="mnemos-edit-comment-save" '
        + 'class="primary">' + escapeHtml(t("Save")) + '</button>'
        + '</menu>';
      document.body.appendChild(dlg);
      var ta = dlg.querySelector("textarea");
      ta.value = currentBody || "";
      function _close(value) {
        try { dlg.close(); } catch (_) {}
        try { dlg.remove(); } catch (_) {}
        resolve(value);
      }
      dlg.querySelector("#mnemos-edit-comment-cancel")
        .addEventListener("click", function () { _close(null); });
      dlg.querySelector("#mnemos-edit-comment-save")
        .addEventListener("click", function () {
          var v = ta.value.trim();
          _close(v || null);
        });
      dlg.addEventListener("cancel", function (ev) {
        ev.preventDefault();
        _close(null);
      });
      ta.addEventListener("keydown", function (ev) {
        // Cmd/Ctrl+Enter = Save, matches GitHub's comment textarea.
        if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") {
          ev.preventDefault();
          var v = ta.value.trim();
          _close(v || null);
        }
      });
      try { dlg.showModal(); } catch (_) { dlg.setAttribute("open", ""); }
      setTimeout(function () { ta.focus(); }, 0);
    });
  }

  window._mnemosEditComment = async function (commentId, kind, targetId) {
    // Fetch current body so the textarea pre-fills (the UX nit from
    // the original prompt() — operators had to retype everything).
    var current = "";
    try {
      var existing = await fetch("/api/v1/comments/" + commentId);
      if (existing.ok) {
        var row = await existing.json();
        current = row.body || "";
      }
    } catch (_) {}
    var fresh = await _editCommentDialog(current);
    if (fresh == null || !fresh.trim()) return;
    var r = await fetch("/api/v1/comments/" + commentId, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ body: fresh }),
    });
    if (!r.ok) { await showError("Edit failed", r); return; }
    var container = document.querySelector('[data-comment-thread][data-target="' + targetId + '"]');
    if (container && container._mnemosReload) await container._mnemosReload();
  };

  window._mnemosDeleteComment = async function (commentId, kind, targetId) {
    if (!confirm("Delete this comment?")) return;
    var r = await fetch("/api/v1/comments/" + commentId, { method: "DELETE" });
    if (!r.ok && r.status !== 204) { await showError("Delete failed", r); return; }
    var container = document.querySelector('[data-comment-thread][data-target="' + targetId + '"]');
    if (container && container._mnemosReload) await container._mnemosReload();
  };

  // ─── Notification centre (PR-41) ──────────────────────────────────
  //
  // A minimal in-browser inbox so multi-operator teams have a single
  // surface where "analysis completed", "diff approved", "your
  // permissions changed" land — instead of toast soup that flashes
  // and disappears.
  //
  // MVP storage model: per-origin localStorage. Server-pushed
  // notifications (the eventual SSE / WebSocket fan-out) are a
  // Phase-3 follow-up; for now the platform's own front-end calls
  // ``MnemosUI.notify(...)`` whenever it sees something the operator
  // would want to see in the inbox.
  //
  // A notification is: { id, title, body, level, at, read }.
  //   level ∈ {info, success, warn, error}
  //   at    = epoch ms
  //
  // The unread count drives the bell badge; clicking a notification
  // marks it read. The "Clear all" action wipes the list.
  // Cross-tab sync uses the same BroadcastChannel pattern as the
  // SSE strip so a notification fired from /analysis is visible on
  // /findings without a reload.

  var _NOTIF_KEY = "mnemos_notifications";
  var _NOTIF_MAX = 50;  // cap to keep localStorage from growing
  var _notifChannel = null;

  function _notifBC() {
    if (_notifChannel) return _notifChannel;
    if (typeof BroadcastChannel === "undefined") return null;
    try {
      _notifChannel = new BroadcastChannel("mnemos-notifs");
    } catch (_) { _notifChannel = null; }
    return _notifChannel;
  }

  function readNotifications() {
    try {
      var raw = localStorage.getItem(_NOTIF_KEY);
      return raw ? JSON.parse(raw) : [];
    } catch (_) { return []; }
  }

  function _writeNotifications(list) {
    try {
      var trimmed = list.slice(0, _NOTIF_MAX);
      localStorage.setItem(_NOTIF_KEY, JSON.stringify(trimmed));
      _renderBell();
      var ch = _notifBC();
      if (ch) try { ch.postMessage({ type: "updated" }); } catch (_) {}
    } catch (_) {}
  }

  function notify(title, opts) {
    opts = opts || {};
    var entry = {
      id: (Date.now().toString(36) + Math.random().toString(36).slice(2, 8)),
      title: String(title || ""),
      body: String(opts.body || ""),
      level: opts.level || "info",
      at: Date.now(),
      read: false,
    };
    var list = readNotifications();
    list.unshift(entry);
    _writeNotifications(list);
    // Also fire a toast so the new notification is visible right
    // away — the bell badge is a *secondary* surface.
    if (opts.silent !== true) {
      showToast(entry.title, entry.level === "info" ? null : entry.level);
    }
  }

  function clearNotifications() { _writeNotifications([]); }

  function _renderBell() {
    var bell = document.getElementById("notif-bell");
    var badge = document.getElementById("notif-badge");
    var list = document.getElementById("notif-list");
    if (!bell || !badge || !list) return;
    var notifs = readNotifications();
    var unread = notifs.filter(function (n) { return !n.read; }).length;
    badge.hidden = unread === 0;
    badge.textContent = unread > 9 ? "9+" : String(unread);
    if (!notifs.length) {
      list.innerHTML = '<li class="notif-empty">' +
        (typeof t === "function" ? t("No notifications yet.") : "No notifications yet.") +
        '</li>';
      return;
    }
    list.innerHTML = notifs.slice(0, 20).map(function (n) {
      var when = typeof relativeTime === "function"
        ? relativeTime(new Date(n.at).toISOString())
        : new Date(n.at).toLocaleString();
      return '<li class="notif-item ' + (n.read ? "read" : "unread") + " " + n.level + '" data-id="' + n.id + '">' +
        '<div class="notif-row">' +
          '<strong>' + escapeHtml(n.title) + '</strong>' +
          '<span class="muted notif-when">' + escapeHtml(when) + '</span>' +
        '</div>' +
        (n.body ? '<div class="notif-body muted">' + escapeHtml(n.body) + '</div>' : "") +
      '</li>';
    }).join("");
    // Mark-as-read on click.
    list.querySelectorAll(".notif-item").forEach(function (li) {
      li.addEventListener("click", function () {
        var id = li.getAttribute("data-id");
        var updated = readNotifications().map(function (n) {
          return n.id === id ? Object.assign({}, n, { read: true }) : n;
        });
        _writeNotifications(updated);
      });
    });
  }

  if (typeof document !== "undefined") {
    document.addEventListener("DOMContentLoaded", function () {
      _renderBell();
      // Cross-tab refresh: a notification posted by /analysis should
      // light the bell on /findings without a reload.
      var ch = _notifBC();
      if (ch) ch.onmessage = function () { _renderBell(); };
      // Bell click toggles the dropdown.
      var bell = document.getElementById("notif-bell");
      var panel = document.getElementById("notif-panel");
      if (bell && panel) {
        bell.addEventListener("click", function (ev) {
          ev.stopPropagation();
          panel.classList.toggle("open");
        });
        // Click outside closes.
        document.addEventListener("click", function () {
          panel.classList.remove("open");
        });
        panel.addEventListener("click", function (ev) { ev.stopPropagation(); });
        var clearBtn = document.getElementById("notif-clear");
        if (clearBtn) clearBtn.addEventListener("click", clearNotifications);
      }
    });
  }

  // ─── SSE cross-tab status (P2-8) ─────────────────────────────────────
  // When the Analysis tab's EventSource goes live or drops out, the
  // operator on a different tab has no way to know. PR-17 added the
  // ``#sse-status`` pill on /analysis only; this layer broadcasts
  // state changes through ``BroadcastChannel("mnemos-sse")`` so a
  // sticky strip at the top of every page can mirror them.
  //
  // Publisher: analysis.html's monitor()/openStream() call
  // ``publishSseState(state)``. Subscriber: every page that loads
  // _layout.html (which includes a hidden ``#sse-cross-tab-strip``
  // ready to be revealed).

  var _sseChannel = null;
  function _bc() {
    if (_sseChannel) return _sseChannel;
    if (typeof BroadcastChannel === "undefined") return null;
    try {
      _sseChannel = new BroadcastChannel("mnemos-sse");
    } catch (_) {
      _sseChannel = null;
    }
    return _sseChannel;
  }

  function publishSseState(state) {
    // 8th-round audit Critical UX-2: BroadcastChannel only delivers
    // *future* messages, so a tab opened mid-disconnect has no way
    // to know the stream is down. We also stash the latest state in
    // localStorage so a fresh tab can read it on DOMContentLoaded
    // and reveal the cross-tab strip without waiting for the next
    // publish.
    try {
      localStorage.setItem(
        "mnemos_sse_last_state",
        JSON.stringify({ state: state, at: Date.now() }),
      );
    } catch (_) {}
    var ch = _bc();
    if (!ch) return;
    try { ch.postMessage({ state: state, at: Date.now() }); } catch (_) {}
  }

  function _readPersistedSseState() {
    try {
      var raw = localStorage.getItem("mnemos_sse_last_state");
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      // Stale ``disconnected`` reads (older than 30 minutes) are
      // ignored — if the analysis tab actually closed cleanly we'd
      // expect ``idle`` to have been written, but operators do
      // close laptops. Don't lie to fresh tabs.
      if (Date.now() - (parsed.at || 0) > 30 * 60 * 1000) return null;
      return parsed.state;
    } catch (_) {
      return null;
    }
  }

  function _applySseStrip(state) {
    var strip = document.getElementById("sse-cross-tab-strip");
    if (!strip) return;
    // The strip stays hidden in two cases:
    //   1. The current page is /analysis — the in-tab badge is
    //      authoritative there, so a second indicator would be noise.
    //   2. The last broadcast said "live" — a green strip on every
    //      page would just be visual clutter.
    var onAnalysis = window.location.pathname === "/analysis";
    if (onAnalysis || state === "live" || state === "idle") {
      strip.hidden = true;
      return;
    }
    strip.hidden = false;
  }

  if (typeof document !== "undefined") {
    document.addEventListener("DOMContentLoaded", function () {
      // Replay the last state a publisher persisted so a tab opened
      // mid-disconnect lights up its strip immediately.
      var persisted = _readPersistedSseState();
      if (persisted) _applySseStrip(persisted);
      var ch = _bc();
      if (!ch) return;
      ch.onmessage = function (ev) {
        if (!ev || !ev.data) return;
        _applySseStrip(ev.data.state);
      };
    });
  }

  // ─── i18n (P2-3) ─────────────────────────────────────────────────────
  // Tiny phrase-book layer. No build step, no library. The platform
  // ships English as canonical and Korean as the first translation —
  // the spec's "1인 친화" goal targets Korean operators specifically.
  //
  // Locale resolution priority:
  //   1. ``localStorage["mnemos_locale"]`` (operator preference)
  //   2. ``navigator.language`` if it starts with ``ko``
  //   3. ``en`` fallback
  //
  // Each phrase is keyed by an English string so a template that
  // hasn't been i18n-ified yet just returns its argument unchanged.
  // This lets us migrate page by page without breaking the others.

  var _phraseBookKo = {
    // Toasts
    "Project \"$1\" created. Ready to analyse it?":
      "프로젝트 \"$1\"가 등록되었습니다. 분석을 시작할까요?",
    "Analysis run completed.": "분석이 완료되었습니다.",
    "Findings rebuild queued.": "Findings 재구축이 예약되었습니다.",
    "Approve failed": "승인 실패",
    "Grant refused": "권한 거부됨",
    // Voice command input (PR-155)
    "Ask by voice": "음성으로 질문",
    "Voice input": "음성 입력",
    "Start voice input": "음성 입력 시작",
    "Stop recording": "녹음 중지",
    "Speak your question (local recognition: $1)":
      "음성으로 질문하세요 (로컬 인식: $1)",
    "Recognised: $1": "인식됨: $1",
    "Transcription failed": "음성 인식 실패",
    "Didn't catch that — please try again.":
      "음성을 인식하지 못했습니다 — 다시 시도해 주세요.",
    "Microphone access was blocked. Allow it in your browser to use voice input.":
      "마이크 접근이 차단되었습니다. 음성 입력을 사용하려면 브라우저에서 허용하세요.",
    "Voice recognition isn't enabled on this server.":
      "이 서버에서는 음성 인식이 활성화되어 있지 않습니다.",
    // Voice output / TTS (PR-156)
    "Listen": "듣기",
    "Read the answer aloud": "답변을 음성으로 읽기",
    "Speech failed": "음성 출력 실패",
    "Voice output isn't enabled on this server.":
      "이 서버에서는 음성 출력이 활성화되어 있지 않습니다.",
    "Live analysis stream disconnected after 6 retries. Click the Monitor button to start a fresh stream, or reload the page to start over.":
      "분석 스트림 연결이 6회 재시도 후 끊어졌습니다. Monitor 버튼을 다시 누르거나 페이지를 새로고침하세요.",
    // Onboarding card
    "Welcome to Mnemos": "Mnemos에 오신 것을 환영합니다",
    "Register a GitLab project": "GitLab 프로젝트 등록",
    // PR-57 — guided tour + help.
    "Skip": "건너뛰기",
    "Back": "이전",
    "Next": "다음",
    "Done": "완료",
    "Help": "도움말",
    "Replay welcome tour": "환영 투어 다시 보기",
    "Replay the guided tour of the main dashboard surfaces.":
      "주요 대시보드 화면의 가이드 투어를 다시 봅니다.",
    "Mnemos turns a large multi-language codebase into a queryable knowledge graph, then flags risks as prioritised findings. This 4-step tour shows the main surfaces.":
      "Mnemos 는 거대한 다중 언어 코드베이스를 조회 가능한 지식 그래프로 만들고, 위험을 우선순위가 매겨진 발견 항목으로 표시합니다. 이 4단계 투어가 주요 화면을 안내합니다.",
    "1. Register a project": "1. 프로젝트 등록",
    "Start here — add a GitLab project. Mnemos clones it, runs the analyzers, and builds the graph.":
      "여기서 시작하세요 — GitLab 프로젝트를 추가합니다. Mnemos 가 클론하고, 분석기를 실행하고, 그래프를 만듭니다.",
    "2. Review findings": "2. 발견 항목 검토",
    "Findings are risks the analysis surfaced — sorted by a 0-100 risk score. Each one carries a suggested fix and a one-click 'Create plan' button.":
      "발견 항목은 분석이 드러낸 위험으로, 0-100 위험 점수로 정렬됩니다. 각 항목은 권장 조치와 원클릭 '계획 생성' 버튼을 제공합니다.",
    "3. Explore the graph": "3. 그래프 탐색",
    "The Graph tab visualises components and their CALLS / EXPOSES relationships. Solid edges are confirmed by runtime traces.":
      "Graph 탭은 컴포넌트와 CALLS / EXPOSES 관계를 시각화합니다. 실선 엣지는 런타임 트레이스로 확인된 것입니다.",
    "4. Share the report": "4. 보고서 공유",
    "The Report tab generates a printable one-pager — health, trend, and the system-level narrative — for a PM or lead.":
      "Report 탭은 PM 또는 리드를 위한 인쇄 가능한 1페이지 보고서 — 건강, 추이, 시스템 수준 서술 — 를 생성합니다.",
    "Run the first analysis": "첫 분석 실행",
    "Review the results": "결과 검토",
    // PR-103 — "Triage now" landing card.
    "Triage now": "지금 처리",
    "Top open P1 findings — fix these first.":
      "현재 열려 있는 P1 발견 항목 — 이것부터 해결하세요.",
    // PR-106 — RBAC role-gated tooltip.
    "$1 role required.": "$1 권한이 필요합니다.",
    // PR-107 — health UI page.
    "Platform health": "플랫폼 상태",
    "Health": "상태",
    "Refresh": "새로고침",
    "Overall": "전체",
    "Operational counts (24h)": "운영 카운트 (24시간)",
    "Metric": "지표",
    "Value": "값",
    // PR-113 — docs index page.
    "Docs": "문서",
    "Documentation": "문서",
    "Operator guides": "운영자 가이드",
    "Design docs": "설계 문서",
    "Pick a document on the left.": "왼쪽에서 문서를 선택하세요.",
    "Document not found.": "문서를 찾을 수 없습니다.",
    "Failed to load document.": "문서 불러오기 실패.",
    "docs-blurb":
      "플랫폼 자체 파일시스템에서 가져온 문서. 운영자 가이드부터 시작하고, 설계 문서로 내부 동작을 이해하세요.",
    "health-blurb":
      "/api/v1/health/ready 가 실행하는 5개의 readiness 체크 실시간 표시. DB / Redis / worker 실패는 전체 상태를 degraded (503) 로 전환; analyzer binary 누락은 advisory — 플랫폼은 동작하지만 해당 언어는 분석 안 됨.",
    // PR-129 — heading hierarchy (screen-reader-only h2 wrappers).
    "Findings health panel": "발견 항목 건강 패널",
    "Graph statistics": "그래프 통계",
    // Empty / status text
    "No analysis runs yet. Start one from the Analysis tab.":
      "아직 분석 실행이 없습니다. Analysis 탭에서 시작하세요.",
    "No projects in your organisation.": "조직에 등록된 프로젝트가 없습니다.",
    "No data entities yet.": "데이터 엔티티가 없습니다.",
    "No findings.": "결과가 없습니다.",
    "No runs yet.": "분석 실행이 없습니다.",
    "No rows returned.": "반환된 행이 없습니다.",
    "Loading…": "로딩 중…",
    "Findings": "결과",
    "Projects": "프로젝트",
    "Analysis": "분석",
    "Data": "데이터",
    "Plans": "계획",
    "Diffs": "변경 검토",
    "Audit": "감사",
    "Settings": "설정",
    "Dashboard": "대시보드",
    "Audit log": "감사 로그",
    "Plans (Gate A)": "계획 (Gate A)",
    "Diffs (Gate B)": "변경 검토 (Gate B)",
    "Data entities": "데이터 엔티티",
    "Organizations": "조직",
    "SSO / OIDC": "SSO / OIDC",
    "GDPR tools": "GDPR 도구",
    "Admin": "관리자",
    // PR-165 — sidebar section headers (Analysis/Admin already above).
    "Explore": "탐색",
    "Governance": "거버넌스",
    "System": "시스템",
    "More": "더보기",
    "(GitLab path or service-registry id)": "(GitLab 경로 또는 서비스 레지스트리 ID)",
    "(audit log requires ≥ 3 chars; ≥ 10 strongly recommended)": "(감사 로그에는 최소 3자; 10자 이상 강력 권장)",
    "(blank = platform default 10 000)": "(비워두면 플랫폼 기본값 10,000)",
    "(cron expression per line)": "(줄당 cron 표현식)",
    "(decrypted connection string source)": "(복호화된 연결 문자열 소스)",
    "(one per line)": "(한 줄에 하나씩)",
    "(regex per line, redact entire column)": "(줄당 정규식, 열 전체 가림)",
    "(regex per line, redact most chars)": "(줄당 정규식, 대부분 문자 가림)",
    "(so tenant boundaries don't leak existence).": "(테넌트 경계가 존재 여부를 노출하지 않도록).",
    "0 / 200 chars": "0 / 200자",
    "AWR": "AWR",
    "Add a new binding": "새 바인딩 추가",
    "Allow Oracle AWR queries": "Oracle AWR 쿼리 허용",
    "Analyse": "분석",
    "Analyzers": "분석기",
    "Approve with break-glass token": "긴급 권한 토큰으로 승인",
    "Approved — GitLab MR created.": "승인됨 — GitLab MR이 생성되었습니다.",
    "Branch": "브랜치",
    "Break-glass token expired.": "긴급 권한 토큰이 만료되었습니다.",
    "Break-glass token issued": "긴급 권한 토큰 발급됨",
    "Callback route:": "콜백 경로:",
    "Certainty": "확신도",
    "Click load.": "불러오기를 클릭하세요.",
    "Click refresh to load.": "새로고침을 클릭하여 불러오세요.",
    "Click refresh.": "새로고침을 클릭하세요.",
    "Component": "컴포넌트",
    "Component ID": "컴포넌트 ID",
    "Component id copied into the SQL form below.": "컴포넌트 ID가 아래 SQL 폼에 복사되었습니다.",
    "Copy": "복사",
    "Copy failed — select the URL manually.": "복사 실패 — URL을 직접 선택하세요.",
    "Copy failed — select the token manually and copy with Ctrl/Cmd-C.": "복사 실패 — 토큰을 직접 선택한 후 Ctrl/Cmd-C로 복사하세요.",
    "Create one in the Secrets section above first (kind = db_connection) — registration will be refused until a secret is wired up.": "위의 시크릿 섹션에서 먼저 생성하세요 (kind = db_connection) — 시크릿이 연결될 때까지 등록이 거부됩니다.",
    "Create organisation": "조직 생성",
    "Create project": "프로젝트 만들기",
    "Create secret": "시크릿 생성",
    "Created": "생성일",
    "Crypto": "암호화",
    "DB component ID": "DB 컴포넌트 ID",
    "DB · Redis · worker OK": "DB · Redis · 워커 정상",
    "DEGRADED": "저하됨",
    "Database": "데이터베이스",
    "Delete": "삭제",
    "Delete failed": "삭제에 실패했습니다",
    "Delete organisation \"$1\"? Existing rows that reference it will keep their dangling FK — run cleanup manually.": "조직 \"$1\"을(를) 삭제하시겠습니까? 해당 조직을 참조하는 기존 행은 끊어진 외래 키를 유지합니다 — 수동으로 정리하세요.",
    "Delete secret \"$1\"?": "시크릿 \"$1\"을(를) 삭제하시겠습니까?",
    "Deletes the user + API keys, rewrites audit entries so the actor field becomes redacted:<uuid>. You cannot delete yourself — an admin locking their own session mid-flight would be catastrophic.": "사용자 및 API 키를 삭제하고, 감사 항목의 행위자 필드를 redacted:<uuid>로 재작성합니다. 자기 자신은 삭제할 수 없습니다 — 관리자가 자신의 세션을 잠그면 치명적인 문제가 발생할 수 있습니다.",
    "Diff": "변경 내용",
    "Display": "표시 이름",
    "Encrypted at rest via the configured KMS backend. Only the metadata is ever returned by this API — the plaintext value is accessible only to the analyzer containers that need it.": "설정된 KMS 백엔드를 통해 저장 시 암호화됩니다. 이 API는 메타데이터만 반환하며, 평문 값은 필요한 분석기 컨테이너만 접근할 수 있습니다.",
    "Entity ID": "엔티티 ID",
    "Erase": "삭제",
    "Erase user": "사용자 삭제",
    "Erase user $1? This cannot be undone.": "사용자 $1을(를) 삭제하시겠습니까? 이 작업은 되돌릴 수 없습니다.",
    "Existing organisations": "기존 조직",
    "Export as JSON": "JSON으로 내보내기",
    "Export user data": "사용자 데이터 내보내기",
    "Failed to load DB bindings: HTTP $1": "DB 바인딩 불러오기 실패: HTTP $1",
    "GitLab pushes ingested →": "GitLab 푸시 수신됨 →",
    "ID": "ID",
    "Invalid masking regex (server would silently skip these): $1": "유효하지 않은 마스킹 정규식 (서버가 조용히 건너뜁니다): $1",
    "Investigate →": "조사 →",
    "Issue break-glass grant": "긴급 권한 발급",
    "Issue break-glass grant (admin)": "긴급 권한 발급 (관리자)",
    "Issue grant": "권한 발급",
    "Last tested": "마지막 테스트",
    "Latency load failed": "지연 정보 불러오기 실패",
    "Load entities": "엔티티 불러오기",
    "Load entities first to set the project id.": "먼저 엔티티를 불러와 프로젝트 ID를 설정하세요.",
    "Load existing bindings": "기존 바인딩 불러오기",
    "Load sample": "샘플 불러오기",
    "Loaded shared approval context (submission $1…). Paste the token from the OOB channel.": "공유된 승인 컨텍스트가 로드되었습니다 (제출 $1…). 대역 외 채널에서 토큰을 붙여넣으세요.",
    "Login trigger:": "로그인 트리거:",
    "Maintenance windows": "유지보수 시간대",
    "Manage →": "관리 →",
    "Masking rules — full": "마스킹 규칙 — 전체",
    "Masking rules — partial": "마스킹 규칙 — 부분",
    "Max rows": "최대 행 수",
    "Monitor": "모니터",
    "Multi-tenant partitioning. Every project, user, and secret is scoped to exactly one organisation; routes that mention": "멀티 테넌트 파티셔닝. 모든 프로젝트, 사용자, 시크릿은 정확히 하나의 조직에 속합니다. 다음을 언급하는 경로는",
    "Next:": "다음:",
    "No $1 submissions in your organisation.": "조직 내 $1 제출이 없습니다.",
    "No db_connection secret yet.": "아직 db_connection 시크릿이 없습니다.",
    "No organisations recorded.": "등록된 조직이 없습니다.",
    "No rows in sample.": "샘플에 행이 없습니다.",
    "No secrets stored.": "저장된 시크릿이 없습니다.",
    "No stages recorded yet.": "아직 기록된 단계가 없습니다.",
    "OIDC is configured via environment variables on the platform container so credentials never land in the database. Toggle a restart after changing": "OIDC는 플랫폼 컨테이너의 환경 변수로 구성되므로 자격 증명이 데이터베이스에 저장되지 않습니다. 변경 후 재시작하세요:",
    "OIDC state:": "OIDC 상태:",
    "Open /api/v1/auth/oidc/login": "/api/v1/auth/oidc/login 열기",
    "Open Analysis →": "분석 열기 →",
    "Open Findings →": "결과 열기 →",
    "Open the login URL in a new tab — you'll be redirected to your IdP and back. First-time users are provisioned as": "새 탭에서 로그인 URL을 열면 IdP로 리디렉션된 후 돌아옵니다. 최초 사용자는 다음 권한으로 프로비저닝됩니다:",
    "Paste the break-glass token first.": "먼저 긴급 권한 토큰을 붙여넣으세요.",
    "Paste the token an admin issued for this submission.": "이 제출에 대해 관리자가 발급한 토큰을 붙여넣으세요.",
    "Per-project DB bindings carry sensitive_tables, masking_rules, allow_awr, and maintenance_windows (spec §12.2). The platform refuses to bind a credential that the analyzer's db_probe verb cannot prove is read-only.": "프로젝트별 DB 바인딩에는 sensitive_tables, masking_rules, allow_awr, maintenance_windows(스펙 §12.2)가 포함됩니다. 플랫폼은 분석기의 db_probe 동사가 읽기 전용임을 증명하지 못하는 자격 증명의 바인딩을 거부합니다.",
    "Pick a project.": "프로젝트를 선택하세요.",
    "Probe failed — registration refused": "프로브 실패 — 등록 거부됨",
    "Project DB bound successfully.": "프로젝트 DB가 성공적으로 바인딩되었습니다.",
    "Projects in your organisation": "조직의 프로젝트",
    "Pulled directly from Postgres so every browser sees the same numbers, unlike worker-process Prometheus counters.": "워커 프로세스 Prometheus 카운터와 달리, Postgres에서 직접 가져오므로 모든 브라우저가 동일한 수치를 확인합니다.",
    "Purpose": "목적",
    "Query authorised and logged.": "쿼리가 승인되어 기록되었습니다.",
    "Query authorised and written to the audit log. The platform does not run SQL directly — hand the rewritten statement to the analyzer, then view masked rows in the Sample viewer above.": "쿼리가 승인되어 감사 로그에 기록되었습니다. 플랫폼은 SQL을 직접 실행하지 않습니다 — 재작성된 구문을 분석기에 전달한 뒤, 위의 샘플 뷰어에서 마스킹된 행을 확인하세요.",
    "Query refused": "쿼리 거부됨",
    "READY": "준비됨",
    "Rationale (≥ 200 chars)": "근거 (200자 이상)",
    "Rationale must be at least 200 characters.": "근거는 최소 200자 이상이어야 합니다.",
    "Raw event stream": "원시 이벤트 스트림",
    "Raw export data (JSON)": "원시 내보내기 데이터 (JSON)",
    "Redis": "Redis",
    "Refresh list": "목록 새로고침",
    "Register (with probe)": "등록 (프로브 포함)",
    "Registration failed": "등록 실패",
    "Required env vars": "필수 환경 변수",
    "Response": "응답",
    "Results capped at $1 rows (you asked for $2).": "결과가 $1행으로 제한되었습니다 (요청: $2행).",
    "Rewritten SQL (clamped, write-blocked)": "재작성된 SQL (제한됨, 쓰기 차단)",
    "Run query": "쿼리 실행",
    "Runtime status": "런타임 상태",
    "SHA": "SHA",
    "SQL": "SQL",
    "SQL parse failed — ": "SQL 파싱 실패 — ",
    "SQL query — validate & authorise (read-only)": "SQL 쿼리 — 검증 및 승인 (읽기 전용)",
    "Sample viewer": "샘플 뷰어",
    "Secret test failed": "시크릿 테스트 실패",
    "Secret test passed.": "시크릿 테스트가 통과되었습니다.",
    "Select a kind to see the expected connection string format.": "종류를 선택하면 예상 연결 문자열 형식이 표시됩니다.",
    "Sensitive": "민감 여부",
    "Sensitive tables": "민감 테이블",
    "Share link (no authority — only loads the right form)": "공유 링크 (권한 없음 — 올바른 폼만 로드)",
    "Share link copied.": "공유 링크가 복사되었습니다.",
    "Show next $1 rows": "다음 $1행 보기",
    "Showing $1 of $2 rows.": "$2행 중 $1행 표시 중.",
    "Slug": "슬러그",
    "Spec §2.5 — the analyzer's db_probe verb did not confirm read-only access for this credential. Details below come straight from the analyzer; the binding has not been persisted.": "스펙 §2.5 — 분석기의 db_probe 동사가 이 자격 증명의 읽기 전용 접근을 확인하지 못했습니다. 아래 상세 정보는 분석기에서 직접 전달된 것이며, 바인딩은 저장되지 않았습니다.",
    "Start the first analysis run →": "첫 분석 실행 시작 →",
    "Started": "시작됨",
    "Stats": "통계",
    "Step 2 of 3:": "3단계 중 2단계:",
    "Stop": "중지",
    "Submission rejected.": "제출이 반려되었습니다.",
    "Supervisory-authority-ready right-to-access and right-to-erasure. Every action emits a distinct audit entry so the forensic chain survives even after the subject user row is removed.": "감독 기관 대응 가능한 접근권 및 삭제권 도구입니다. 모든 작업은 고유한 감사 항목을 생성하므로, 대상 사용자 행이 삭제된 후에도 포렌식 체인이 유지됩니다.",
    "Task": "작업",
    "Test": "테스트",
    "Test result": "테스트 결과",
    "Test the flow": "플로우 테스트",
    "The default organisation is the home of all single-tenant deployments and cannot be deleted.": "기본 조직은 단일 테넌트 배포의 기본 공간이며 삭제할 수 없습니다.",
    "Three steps to get to your first finding:": "첫 번째 결과를 얻기까지 3단계:",
    "Token": "토큰",
    "Token (carries authority — copy via secure channel)": "토큰 (권한 포함 — 보안 채널로 복사)",
    "Token copied to clipboard.": "토큰이 클립보드에 복사되었습니다.",
    "Trigger the first analysis run. The project ID is pre-filled below — just press Start analysis.": "첫 분석 실행을 시작하세요. 프로젝트 ID가 아래에 미리 채워져 있습니다 — 분석 시작을 누르세요.",
    "Triggered by": "실행 주체",
    "Use in query →": "쿼리에 사용 →",
    "User ID": "사용자 ID",
    "Waiting for stage events…": "단계 이벤트 대기 중…",
    "Windows": "유지보수 시간대",
    "Worker": "워커",
    "continuation (skip L0; summaries only)": "이어서 (L0 건너뜀; 요약만)",
    "degraded": "저하됨",
    "degraded:": "저하됨:",
    "done": "완료",
    "error": "오류",
    "fail": "실패",
    "finding": "결과",
    "findings": "결과",
    "full (L0 extract + findings + L1–L3)": "전체 (L0 추출 + 결과 + L1–L3)",
    "grant-dialog-blurb": "현재 diff에 대해 Ultrareview가 다시 실행됩니다. 판정이 여전히 blocked이면 권한 발급이 거부됩니다. 근거는 감사 로그에 그대로 저장됩니다.",
    "in the default organisation; promote them from the user management tab.": "기본 조직에 배치됩니다. 사용자 관리 탭에서 권한을 승격할 수 있습니다.",
    "masking applied": "마스킹 적용됨",
    "masking applied — PII tokens visible in raw SQL response only": "마스킹 적용됨 — PII 토큰은 원본 SQL 응답에서만 확인 가능",
    "needs break-glass grant": "긴급 권한 필요",
    "no": "아니요",
    "no DBs bound": "바인딩된 DB 없음",
    "no db_connection secrets exist": "db_connection 시크릿이 없습니다",
    "none": "없음",
    "ok": "정상",
    "probe sweep verdict →": "프로브 점검 결과 →",
    "protected": "보호됨",
    "query-runner-blurb": "플랫폼은 프로덕션 DB에 직접 연결하지 않습니다 (스펙 §2.5/§2.8). 이 폼은 SELECT를 검증하고 승인합니다. SQLGlot이 상태를 변경하는 구문을 차단하고, 과도한 LIMIT를 조정하며, DB별 정책(민감 테이블, AWR 동의, 유지보수 창)을 적용하고, 요청을 감사 로그에 기록합니다. 분석기가 실행할 재작성된 SQL을 반환하며, 마스킹된 행은 분석기 보고 후 위의 샘플 뷰어에 표시됩니다.",
    "read-only": "읽기 전용",
    "reject access from users in a different organisation with": "다른 조직의 사용자 접근을 다음 코드로 거부합니다:",
    "select": "선택",
    "share-url-blurb": "링크에는 제출 ID와 6자리 권한 지문이 URL 프래그먼트에 포함되어 서버 접근 로그에 기록되지 않습니다. 운영자 확인 후 브라우저 방문 기록을 삭제하세요.",
    "token-dialog-blurb": "토큰을 다른 운영자에게 대역 외 채널(Slack DM, 음성 통화 등)로 전달하세요. 공유 링크는 별도로 전송하세요 — 클릭하면 올바른 제출이 로드된 승인 다이얼로그가 열립니다. 만료까지 15:00. 1회 사용. 발급자(본인)는 사용할 수 없습니다.",
    "unconsumed grants (15-min TTL) →": "미사용 권한 (15분 TTL) →",
    "yes": "예",
    "Runs": "실행",
    "Chat": "대화",
    "chat-blurb": "선택한 프로젝트의 분석에 대해 무엇이든 물어보세요 — 함수가 하는 일, 쿼리 최적화 방법, 변경 접근법, 프로세스 흐름, 짧은 레포트 초안까지. 답변은 심볼·데이터 접근·호출 그래프·소스 코드에 근거합니다.",
    "Select a project at the top of the sidebar to start chatting.": "대화를 시작하려면 사이드바 상단에서 프로젝트를 선택하세요.",
    "Ask about this project… (Shift+Enter for a new line)": "이 프로젝트에 대해 물어보세요… (Shift+Enter 로 줄바꿈)",
    "Send": "보내기",
    "AI provider": "AI 제공자",
    "(not configured)": "(미설정)",
    "No AI is configured on the server yet.": "아직 서버에 설정된 AI가 없습니다.",
    "Each message calls the AI you selected above.": "메시지마다 위에서 선택한 AI를 호출합니다.",
    "Clear conversation": "대화 지우기",
    "Context": "컨텍스트",
    "Thinking…": "생각 중…",
    "The AI assistant is unavailable (no LLM configured). Use the Ask tab for offline symbol search.": "AI 어시스턴트를 사용할 수 없습니다 (LLM 미설정). 오프라인 심볼 검색은 질의 탭을 사용하세요.",
    "That AI isn't configured on the server. Pick another provider or set its API key.": "해당 AI가 서버에 설정되지 않았습니다. 다른 제공자를 선택하거나 API 키를 설정하세요.",
    "AI providers (Chat)": "AI 제공자 (대화)",
    "ai-providers-blurb": "대화 탭이 사용할 AI를 설정합니다. API 키는 저장 시 암호화되며 다시 표시되지 않습니다. 운영자는 대화마다 제공자를 고르고, '연결 테스트'로 키를 검증하고 계정의 현재 모델 목록을 불러옵니다.",
    "AI provider configuration is admin-only.": "AI 제공자 설정은 관리자 전용입니다.",
    "Configured": "설정됨",
    "Not configured": "미설정",
    "key stored — type to replace": "키 저장됨 — 변경하려면 입력",
    "paste API key": "API 키 입력",
    "Base URL": "Base URL",
    "Agent ID": "에이전트 ID",
    "API key": "API 키",
    "Model": "모델",
    "No key needed — uses the local Claude subscription.": "키 불필요 — 로컬 Claude 구독을 사용합니다.",
    "Clear key": "키 삭제",
    "Test connection": "연결 테스트",
    "Save failed": "저장 실패",
    "Provider saved.": "제공자 저장됨.",
    "Remove the stored API key for this provider?": "이 제공자의 저장된 API 키를 삭제할까요?",
    "Key removed.": "키 삭제됨.",
    "Testing…": "테스트 중…",
    "Test failed": "테스트 실패",
    "Connected": "연결됨",
    "Key error — re-check": "키 오류 — 재확인",
    "Configured (test recommended)": "설정됨 (테스트 권장)",
    "Stored encrypted; not shown again after saving.": "암호화되어 저장되며 저장 후 다시 표시되지 않습니다.",
    "Show/hide key": "키 표시/숨김",
    "Method": "방식",
    "Subscription (Claude Code)": "구독 (Claude Code)",
    "Uses this machine's Claude Code subscription — no API key needed.": "이 기기의 Claude Code 구독을 사용합니다 — API 키 불필요.",
    "Request failed": "요청 실패",
    "Approve": "승인",
    "Reject": "반려",
    "Actor": "행위자",
    "Details": "상세",
    "Detail": "상세 정보",
    "When": "시각",
    "Tasks": "작업",
    "Impact": "영향",
    "No plans yet.": "아직 계획이 없습니다.",
    "No events.": "이벤트가 없습니다.",
    "No submissions for this plan.": "이 계획에 대한 제출이 없습니다.",
    "Could not load plans": "계획을 불러오지 못했습니다",
    "Could not load findings": "결과를 불러오지 못했습니다",
    "Could not load audit events": "감사 이벤트를 불러오지 못했습니다",
    "Could not load submissions": "제출 목록을 불러오지 못했습니다",
    "Could not load data entities": "데이터 엔티티를 불러오지 못했습니다",
    "Could not load runs": "실행 목록을 불러오지 못했습니다",
    "Knowledge Production Platform": "지식 생산 플랫폼",
    "(only if an OIDC provider is configured)": "(OIDC 공급자가 설정된 경우에만)",
    "Analysed on demand": "즉석 분석됨",
    "Flags": "플래그",
    "Could not submit the request. Try again or contact an admin.":
      "요청을 제출하지 못했습니다. 다시 시도하거나 관리자에게 문의하세요.",
    "No matching symbol — try a different phrasing, or set a Source root under Deepen options so Mnemos can analyse the code on demand.":
      "일치하는 심볼이 없습니다 — 다른 표현으로 묻거나, 심화 옵션에서 소스 루트를 설정해 코드를 즉석 분석하세요.",
    "Narrative summaries need an LLM — not configured in local mode.":
      "내러티브 요약은 LLM이 필요합니다 — 로컬 모드에서는 미설정.",
    "No nodes of that kind — showing the full graph instead.":
      "해당 종류의 노드가 없어 — 전체 그래프를 표시합니다.",
    "Sign out": "로그아웃",
    // PR-38 — user management + profile.
    "Profile": "프로필",
    "Users": "유저",
    "Account": "계정",
    "Update your display name, contact email, and avatar. Username and role are managed by an administrator.":
      "표시 이름, 연락 이메일, 아바타를 수정하세요. 사용자명과 역할은 관리자가 관리합니다.",
    "Username": "사용자명",
    "Role": "역할",
    "Display name": "표시 이름",
    "Email": "이메일",
    "Avatar URL": "아바타 URL",
    "Timezone": "시간대",
    "Save profile": "프로필 저장",
    "Change password": "비밀번호 변경",
    "Minimum 12 characters. Sign-in sessions stay valid; only future logins use the new password.":
      "최소 12자. 기존 세션은 유지되며, 이후 로그인부터 새 비밀번호가 적용됩니다.",
    "Current password": "현재 비밀번호",
    "New password": "새 비밀번호",
    "Confirm new password": "새 비밀번호 확인",
    "New password and confirmation do not match.": "새 비밀번호와 확인 값이 일치하지 않습니다.",
    "Profile saved.": "프로필이 저장되었습니다.",
    "Password changed.": "비밀번호가 변경되었습니다.",
    "User management is admin-only.": "유저 관리는 관리자만 사용할 수 있습니다.",
    "Add user": "유저 추가",
    "The new account joins your organisation with the chosen role. Send the temporary password through a secure side-channel; the user can change it from their profile page after first login.":
      "새 계정은 선택한 역할로 조직에 추가됩니다. 임시 비밀번호는 안전한 별도 채널로 전달하세요. 사용자는 첫 로그인 후 프로필 페이지에서 비밀번호를 변경할 수 있습니다.",
    "Temporary password": "임시 비밀번호",
    "Create user": "유저 만들기",
    "Members": "구성원",
    "Include disabled": "비활성 포함",
    "Last login": "마지막 로그인",
    "Active": "활성",
    "Disabled": "비활성",
    "Re-enable": "다시 활성화",
    "Disable": "비활성화",
    "No users yet.": "유저가 없습니다.",
    'User "$1" created.': '유저 "$1"가 생성되었습니다.',
    "Role updated.": "역할이 업데이트되었습니다.",
    "User disabled.": "유저가 비활성화되었습니다.",
    "User re-enabled.": "유저가 다시 활성화되었습니다.",
    // PR-45 — invite / reset / forgot.
    "Forgot password?": "비밀번호 잊으셨나요?",
    "Forgot password": "비밀번호 재설정",
    "Reset password": "비밀번호 재설정",
    "Request reset": "재설정 요청",
    "Set new password": "새 비밀번호 설정",
    "Back to sign in": "로그인으로 돌아가기",
    "Accept invite": "초대 수락",
    "Create account": "계정 만들기",
    "Or invite by token": "또는 토큰으로 초대",
    "Generate invite link": "초대 링크 생성",
    "Copy link": "링크 복사",
    "Link copied.": "링크가 복사되었습니다.",
    "Password": "비밀번호",
    "Enter your username. If an account exists, an admin will receive a reset link to share with you. The platform never sends the token by email automatically.":
      "사용자명을 입력하세요. 계정이 존재하면 관리자가 재설정 링크를 받아 공유합니다. 플랫폼은 이메일을 자동 발송하지 않습니다.",
    "Pick a new password. Minimum 12 characters with at least one letter and one digit.":
      "새 비밀번호를 선택하세요. 최소 12자, 영문자 + 숫자 각 1개 이상.",
    "Choose a username and password to finish setting up your account.":
      "사용자명과 비밀번호를 선택하여 계정 설정을 완료하세요.",
    "Generate an invite token the recipient pastes into the /invite page. They pick their own username and password. The token expires in 7 days and can be used once.":
      "초대 토큰을 생성합니다. 받는 사람이 /invite 페이지에서 토큰을 붙여넣어 사용자명과 비밀번호를 직접 선택합니다. 토큰은 7일 후 만료되며 1회만 사용 가능합니다.",
    "Share this link out-of-band. It expires in 7 days.":
      "안전한 별도 채널로 링크를 전달하세요. 7일 후 만료됩니다.",
    "Reset token is missing from the URL.": "URL 에 재설정 토큰이 없습니다.",
    "Invite token is missing from the URL.": "URL 에 초대 토큰이 없습니다.",
    "Password updated. You can now sign in with the new password.":
      "비밀번호가 변경되었습니다. 새 비밀번호로 로그인할 수 있습니다.",
    "Account created. Redirecting to sign in…": "계정이 생성되었습니다. 로그인 페이지로 이동합니다…",
    "If the account exists, an admin can now retrieve a reset token from the audit log.":
      "계정이 존재한다면 관리자가 감사 로그에서 재설정 토큰을 받을 수 있습니다.",
    // PR-47 — Phase 4 polish.
    "Session expired. Redirecting to sign in…": "세션이 만료되었습니다. 로그인 페이지로 이동합니다…",
    "Export CSV": "CSV 내보내기",
    "Export Excel": "Excel 내보내기",
    "Exported.": "내보냈습니다.",
    "Nothing to export.": "내보낼 항목이 없습니다.",
    "Excel export failed — falling back to CSV is available.":
      "Excel 내보내기 실패 — CSV 내보내기를 대신 사용할 수 있습니다.",
    "Enter a project ID first.": "프로젝트 ID 를 먼저 입력하세요.",
    // PR-49 — knowledge graph visualisation.
    "Graph": "그래프",
    "Knowledge graph": "지식 그래프",
    "Component-level visualisation of the analysed system. Nodes are services / modules; edges are CALLS / EXPOSES / READS / WRITES relationships. Exercised edges (validated by OTLP traces) are drawn solid; static-only edges are dashed.":
      "분석된 시스템의 컴포넌트 수준 시각화. 노드는 서비스/모듈, 엣지는 CALLS/EXPOSES/READS/WRITES 관계입니다. OTLP 트레이스로 확인된 실행된 엣지는 실선으로, 정적 분석만 된 엣지는 점선으로 표시됩니다.",
    "Node kind": "노드 종류",
    "Max nodes": "최대 노드 수",
    "Render": "렌더",
    "Re-layout": "재배치",
    "Nodes": "노드",
    "Edges": "엣지",
    "Verified %": "검증됨 %",
    "Exercised %": "실행됨 %",
    "vs asserted / inferred": "vs 단언/추론",
    "confirmed by OTLP traces": "OTLP 트레이스로 확인됨",
    "Graph truncated — increase Max nodes or filter by kind to see more.":
      "그래프가 잘렸습니다 — 최대 노드 수를 늘리거나 종류로 필터하세요.",
    "Enter a project ID above and click Render to see its knowledge graph.":
      "위에 프로젝트 ID 를 입력하고 Render 를 클릭하면 지식 그래프가 보입니다.",
    "Components": "컴포넌트",
    // PR-67 — human certainty feedback (§1.2).
    "Confirm a graph fact": "그래프 사실 확인",
    "If the analysers only inferred an edge or node, confirming it here records your judgement and raises its certainty from inferred to asserted (spec §1.2). Dispute flags a fact you believe is wrong.":
      "분석기가 엣지나 노드를 추론(inferred)만 한 경우, 여기서 확인하면 귀하의 판단이 기록되고 확신도가 추론에서 단언(asserted)으로 올라갑니다 (스펙 §1.2). 이의 제기는 잘못되었다고 보는 사실을 표시합니다.",
    "Target": "대상",
    "Fact ID": "사실 ID",
    "Action": "동작",
    "Rationale": "근거",
    "Submit": "제출",
    "Why you are confirming or disputing this": "확인 또는 이의 제기 사유",
    "Confirmation failed": "확인 실패",
    "Recorded": "기록됨",
    "Graph fact updated": "그래프 사실이 갱신되었습니다",
    // PR-59 — canvas backend for large graphs.
    "Large graph — rendered on canvas for performance. Hover a node for details.":
      "대규모 그래프 — 성능을 위해 캔버스로 렌더링했습니다. 노드에 마우스를 올리면 상세 정보가 보입니다.",
    // PR-50 — finding risk scoring + remediation.
    "Priority": "우선순위",
    "Risk": "위험도",
    "Kind": "종류",
    "Subject": "대상",
    "Suggested fix": "권장 조치",
    // PR-53 — executive report.
    "Report": "보고서",
    "Executive report": "경영 보고서",
    "A one-page system overview suitable for a PM or an engineering lead. Generate it, then use the browser's print / save-as-PDF.":
      "PM 또는 엔지니어링 리드를 위한 1페이지 시스템 개요. 생성 후 브라우저의 인쇄 / PDF 저장을 사용하세요.",
    "Generate": "생성",
    "Print / PDF": "인쇄 / PDF",
    "System analysis report": "시스템 분석 보고서",
    "Generated": "생성 시각",
    "Health summary": "건강 요약",
    "Risk by priority": "우선순위별 위험",
    "Findings by kind": "종류별 발견",
    "System-level narrative (L3)": "시스템 수준 서술 (L3)",
    "Analysis cost": "분석 비용",
    // PR-71 — report stat-card + L3 labels that were English-only.
    "Mean TTR": "평균 해결 시간",
    "7-day delta": "7일 증감",
    "Open questions:": "미해결 질문:",
    "Show next $1": "다음 $1개 보기",
    "Showing $1 of $2 findings.": "$2개 중 $1개 표시 중.",
    // PR-63 — ROI rollup.
    "Return on investment": "투자 수익 (ROI)",
    "risk points eliminated": "위험 점수 제거됨",
    "risk points still open": "위험 점수 미해결",
    "Risk eliminated per USD": "달러당 제거된 위험",
    "Finding precision": "발견 정밀도",
    "false positive": "오탐",
    // PR-55 — finding trend chart.
    "Finding trend (90 days)": "발견 추이 (90일)",
    "Not enough history yet for a trend chart.": "추이 차트를 그릴 이력이 아직 부족합니다.",
    "new": "신규",
    "resolved": "해결",
    "total over window": "기간 합계",
    "Generated by Mnemos — knowledge production platform.":
      "Mnemos — 지식 생산 플랫폼에서 생성됨.",
    "Enter a project ID and click Generate.": "프로젝트 ID 를 입력하고 생성을 클릭하세요.",
    "No L3 summary yet — run a full analysis to generate the system-level narrative.":
      "L3 요약이 아직 없습니다 — 전체 분석을 실행하여 시스템 수준 서술을 생성하세요.",
    // PR-52 — finding → plan.
    "Create plan": "계획 생성",
    // PR-56 — finding↔plan linkage.
    "linked plan": "연결된 계획",
    "linked plans": "연결된 계획",
    "Create a draft plan from this finding? You can review and edit it on the Plans tab.":
      "이 발견 항목으로 초안 계획을 생성할까요? Plans 탭에서 검토하고 수정할 수 있습니다.",
    "Draft plan created from finding.": "발견 항목으로 초안 계획이 생성되었습니다.",
    "Open the Plans tab to review the new draft?": "Plans 탭을 열어 새 초안을 검토할까요?",
    // PR-51 — system-health panel.
    "Risk index": "위험 지수",
    "mean risk of open findings": "미해결 발견 평균 위험도",
    "P1 open": "P1 미해결",
    "fix these first": "최우선 조치 대상",
    "Open / total": "미해결 / 전체",
    "Mean time to resolve": "평균 해결 시간",
    "Last 7 days": "최근 7일",
    "resolved − new": "해결 − 신규",
    // PR-41 — notification centre + responsive.
    "Notifications": "알림",
    "Clear all": "모두 지우기",
    "No notifications yet.": "알림이 없습니다.",
    "Analysis run failed": "분석 실행 실패",
    "Toggle navigation": "메뉴 열기/닫기",
    // PR-43 — comments.
    "Comments": "댓글",
    "No comments yet.": "댓글이 없습니다.",
    "Write a comment…": "댓글 작성…",
    "Post comment": "댓글 게시",
    "Latest analysis runs": "최근 분석 실행",
    "Recent activity": "최근 활동",
    "No team activity yet.": "팀 활동이 없습니다.",
    // PR-42 — command palette.
    "Search projects, jump to a tab… (cmd/ctrl+K)":
      "프로젝트 검색, 탭 이동… (cmd/ctrl+K)",
    "No matches.": "일치하는 항목이 없습니다.",
    "navigate": "이동",
    "open": "열기",
    "go to projects": "프로젝트로 이동",
    "Recent runs (7d)": "최근 실행 (7일)",
    "Open findings": "미해결 결과",
    "Readiness": "준비 상태",
    "Break-glass active": "긴급 권한 활성",
    "Failed runs (24h)": "실패 (24시간)",
    "Disabled DBs": "비활성화 DB",
    "Webhook events (24h)": "웹훅 이벤트 (24시간)",
    // Placeholders
    "e.g. payments-core": "예: payments-core",
    "https://gitlab.example.com/group/repo": "https://gitlab.example.com/group/repo",
    // Buttons
    "Refresh": "새로고침",
    "Load": "불러오기",
    "Rebuild": "재구축",
    "Start analysis": "분석 시작",
    "Sign in": "로그인",
    // PR-29 — form labels and CTA buttons used across multiple tabs.
    // Keeps the most-clicked surfaces consistent in Korean even
    // though the per-page detailed labels stay English-only for now
    // (see phase2_backlog.md P2-3 follow-up).
    "Search": "검색",
    "Create": "만들기",
    "Load": "불러오기",
    "Load runs": "실행 목록 불러오기",
    "Load submissions": "제출 목록 불러오기",
    "Severity": "심각도",
    "Status": "상태",
    "Kind": "종류",
    "Value": "값",
    "Label": "라벨",
    "Approve → MR": "승인 → MR",
    "Approve with token": "토큰으로 승인",
    "Project ID": "프로젝트 ID",
    "Plan ID": "계획 ID",
    "Run ID": "실행 ID",
    "Source path": "소스 경로",
    "Git SHA": "Git SHA",
    "Scope": "범위",
    "Name": "이름",
    "GitLab project ID": "GitLab 프로젝트 ID",
    "GitLab URL": "GitLab URL",
    "Default branch": "기본 브랜치",
    "Languages": "언어",
    "Actor filter": "행위자 필터",
    "Action filter": "행동 필터",
    "Limit": "최대 개수",
    "Cancel": "취소",
    "Close": "닫기",
    "Trigger run": "분석 시작",
    "Connections (Secrets)": "연결 (비밀)",
    "Project databases": "프로젝트 데이터베이스",
    "Pipeline monitor": "파이프라인 모니터",
    "Recent runs": "최근 실행",
    "Register project": "프로젝트 등록",
    // PR-58 — pipeline latency.
    "Pipeline latency": "파이프라인 지연",
    "Where the analysis time goes, averaged over recent runs. Tune the slowest stage first.":
      "최근 실행 평균 기준 분석 시간 분포. 가장 느린 단계를 먼저 튜닝하세요.",
    "Load latency": "지연 불러오기",
    "No completed runs to measure yet.": "측정할 완료된 실행이 아직 없습니다.",
    "runs analysed": "개 실행 분석됨",
    "mean total": "평균 총시간",
    "slowest": "가장 느림",
    "Stage": "단계",
    "Mean (s)": "평균 (초)",
    "p95 (s)": "p95 (초)",
    "Max (s)": "최대 (초)",
    "Sign in with SSO": "SSO 로 로그인",
    "Loaded shared approval context — paste the token to approve.":
      "공유된 승인 컨텍스트가 로드되었습니다 — 토큰을 붙여넣어 승인하세요.",
    // SSE strip
    "Analysis stream disconnected — open the Analysis tab to reconnect.":
      "분석 스트림 연결 끊김 — Analysis 탭을 열어 재연결하세요.",
    // PR — i18n gap fill: Ask tab, report flows, lifecycle captions,
    // org/secret/project toasts, comment-thread + project-picker labels.
    "A source root is required to trace a process — set it under Deepen options.":
      "프로세스를 추적하려면 소스 루트가 필요합니다 — Deepen options 에서 설정하세요.",
    "Analyse more on demand if the graph can't answer":
      "그래프가 답하지 못하면 필요 시 추가로 분석",
    "Answer":
      "답변",
    "Best match":
      "가장 가까운 항목",
    "No symbol confidently answers this — showing the closest match. Try naming a specific function or file, or set a Source root to analyse on demand.":
      "이 질문에 확실히 답하는 심볼이 없습니다 — 가장 가까운 항목을 보여줍니다. 특정 함수나 파일 이름으로 묻거나, 심화 옵션에서 소스 루트를 설정해 즉석 분석하세요.",
    "Answered":
      "답변됨",
    "Answering…":
      "답변 생성 중…",
    "Answering… (may analyse source files on demand)":
      "답변 생성 중… (필요 시 소스 파일을 분석할 수 있음)",
    "Ask":
      "질의",
    "Ask a question about the analysed system in plain language — Mnemos answers from the knowledge graph (symbols, data access, summaries). If the graph can't answer yet, it analyses the most relevant source files on demand and answers anyway.":
      "분석된 시스템에 대해 자연어로 질문하세요 — Mnemos 가 지식 그래프(심볼, 데이터 접근, 요약)에서 답합니다. 그래프가 아직 답할 수 없으면 가장 관련 있는 소스 파일을 즉석에서 분석해 답합니다.",
    "Ask the system":
      "시스템에 질의",
    "Comment body":
      "댓글 내용",
    "Could not load organisations":
      "조직을 불러오지 못했습니다",
    "Could not load projects":
      "프로젝트를 불러오지 못했습니다",
    "Cross-tier processes (flows)":
      "계층 간 프로세스 (플로우)",
    "Data touched":
      "접근한 데이터",
    "Deepen options":
      "심화 옵션",
    "Deepened (analysed on demand)":
      "심화됨 (즉석 분석)",
    "Diagram render failed: ":
      "다이어그램 렌더링 실패: ",
    "Edit comment":
      "댓글 편집",
    "Erase failed":
      "삭제 실패",
    "Every diff submission moves through this state machine. A blocked verdict can only be cleared by a break-glass grant consumed by a different operator (two-eyes).":
      "모든 diff 제출은 이 상태 기계를 따릅니다. blocked 판정은 다른 운영자가 소비하는 break-glass 권한(2인 확인)으로만 해제할 수 있습니다.",
    "Export failed":
      "내보내기 실패",
    "Finding lifecycle":
      "발견 항목 생명주기",
    "Flags:":
      "플래그:",
    "Gate B lifecycle":
      "Gate B 생명주기",
    "LLM pipeline health":
      "LLM 파이프라인 상태",
    "Live analysis stream disconnected after 6 retries. ":
      "실시간 분석 스트림이 6회 재시도 후 끊겼습니다. ",
    "Microphone access was blocked. Allow it in your ":
      "마이크 접근이 차단되었습니다. 다음에서 허용하세요: ",
    "No confident answer":
      "확신할 수 있는 답변 없음",
    "No matching symbol — try a different phrasing, or provide a source root so Mnemos can analyse on demand.":
      "일치하는 심볼 없음 — 다르게 표현하거나, Mnemos 가 즉석 분석할 수 있도록 소스 루트를 지정하세요.",
    "No process flows yet — run trace_flow on a frontend/backend/DB slice to see the end-to-end process here.":
      "아직 프로세스 플로우가 없습니다 — 프론트엔드/백엔드/DB 구간에 trace_flow 를 실행하면 종단 간 프로세스가 여기 표시됩니다.",
    "No projects in your organisation yet.":
      "아직 조직에 프로젝트가 없습니다.",
    "Organisation created.":
      "조직이 생성되었습니다.",
    "Organisation creation failed":
      "조직 생성 실패",
    "Other matches":
      "다른 일치 항목",
    "Per-project breakdown of how many summaries used the real LLM vs the stub, and why.":
      "프로젝트별로 실제 LLM 과 스텁 중 무엇을 사용한 요약이 몇 건인지와 그 이유를 분석합니다.",
    "Pick a project and enter the process to trace.":
      "프로젝트를 선택하고 추적할 프로세스를 입력하세요.",
    "Plan lifecycle":
      "계획 생명주기",
    "Process trace":
      "프로세스 추적",
    "Project":
      "프로젝트",
    "Project creation failed":
      "프로젝트 생성 실패",
    "Question":
      "질문",
    "Reads":
      "읽기",
    "Register your first project →":
      "첫 프로젝트를 등록하세요 →",
    "Save":
      "저장",
    "Secret creation failed":
      "시크릿 생성 실패",
    "Secret management is admin-only.":
      "시크릿 관리는 관리자 전용입니다.",
    "Secret stored.":
      "시크릿이 저장되었습니다.",
    "Select a project":
      "프로젝트 선택",
    "Source root (enables deepening)":
      "소스 루트 (심화 분석 활성화)",
    "The state machine each plan moves through. Reject feedback loops back to draft so the author can revise. Regenerate re-opens the plan for the analyser to redraft from scratch.":
      "각 계획이 거치는 상태 기계입니다. Reject 의 피드백은 초안으로 되돌아가 작성자가 수정하게 하고, Regenerate 는 분석기가 처음부터 다시 작성하도록 계획을 다시 엽니다.",
    "Token issued. Ask an admin to send it to you, or open the reset link directly:":
      "토큰이 발급되었습니다. 관리자에게 전달을 요청하거나, 재설정 링크를 직접 여세요:",
    "Trace as a process (FE→BE→DB)":
      "프로세스로 추적 (FE→BE→DB)",
    "Traced":
      "추적됨",
    "Tracing the process across tiers… (analyses the relevant FE/BE/DB files)":
      "계층을 가로질러 프로세스 추적 중… (관련 FE/BE/DB 파일을 분석)",
    "Trigger failed":
      "실행 시작 실패",
    "Unknown project":
      "알 수 없는 프로젝트",
    "User erased.":
      "사용자가 삭제되었습니다.",
    "Writes":
      "쓰기",
  };

  var _locale = null;
  function _resolveLocale() {
    if (_locale) return _locale;
    try {
      var pref = localStorage.getItem("mnemos_locale");
      if (pref === "en" || pref === "ko") {
        _locale = pref;
        return _locale;
      }
    } catch (_) {}
    var lang = (typeof navigator !== "undefined" && navigator.language) || "en";
    _locale = lang.toLowerCase().startsWith("ko") ? "ko" : "en";
    return _locale;
  }

  function setLocale(locale) {
    if (locale !== "en" && locale !== "ko") return;
    _locale = locale;
    try { localStorage.setItem("mnemos_locale", locale); } catch (_) {}
    // Re-translate any element marked ``data-i18n`` so a runtime
    // switch updates the UI immediately without a reload.
    applyI18n(document);
  }

  function t(key, vars) {
    var book = _resolveLocale() === "ko" ? _phraseBookKo : null;
    var phrase = (book && book[key]) || key;
    if (vars && typeof vars === "object") {
      var i = 0;
      Object.keys(vars).forEach(function (k) {
        i += 1;
        phrase = phrase.split("$" + i).join(vars[k]);
      });
    }
    return phrase;
  }

  function applyI18n(root) {
    var scope = root || document;
    var nodes = scope.querySelectorAll("[data-i18n]");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var key = el.getAttribute("data-i18n") || el.textContent;
      el.textContent = t(key);
    }
    // Placeholders.
    var ph = scope.querySelectorAll("[data-i18n-placeholder]");
    for (var j = 0; j < ph.length; j++) {
      var pel = ph[j];
      var pkey = pel.getAttribute("data-i18n-placeholder");
      if (pkey) pel.setAttribute("placeholder", t(pkey));
    }
  }

  // ─── PR-106 RBAC visibility ──────────────────────────────────────
  //
  // Pre-PR pattern: pages either rendered an admin-only button
  // unconditionally (so a viewer clicked it and got a 403 toast) or
  // hand-rolled ``window.MNEMOS_USER_ROLE_HINT === 'admin'`` checks
  // inline (so the page silently dropped the button — confusing
  // because the viewer has no clue what they're missing).
  //
  // ``gateByRole`` is the one place that decision lives. An element
  // tagged ``data-required-role="admin"`` (or ``operator``) becomes
  // disabled with a tooltip when the user's role is insufficient.
  // Role precedence: admin > operator > viewer. The platform's
  // backend STILL enforces — this layer only surfaces the wall so
  // the user sees it before they hit it.

  var _ROLE_RANK = { viewer: 1, operator: 2, admin: 3 };

  function _userRoleHint() {
    var r = (typeof window !== "undefined" && window.MNEMOS_USER_ROLE_HINT) || "";
    return String(r).toLowerCase();
  }

  function _roleAllows(actual, required) {
    var a = _ROLE_RANK[actual] || 0;
    var r = _ROLE_RANK[required] || 0;
    return a >= r;
  }

  function gateByRole(root) {
    var scope = root || document;
    var actual = _userRoleHint();
    var nodes = scope.querySelectorAll("[data-required-role]");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var req = (el.getAttribute("data-required-role") || "").toLowerCase();
      if (!req) continue;
      if (_roleAllows(actual, req)) {
        // The user qualifies — restore default state in case a
        // prior gate pass disabled it (e.g. after a role change in
        // the same tab).
        el.removeAttribute("aria-disabled");
        el.classList.remove("role-gated");
        // Only re-enable buttons; an <a> never had a disabled
        // attribute to clear.
        if (el.tagName === "BUTTON" || el.tagName === "INPUT") {
          el.disabled = false;
        }
        if (el.dataset.gatedTitle) {
          el.title = el.dataset.gatedOriginalTitle || "";
          delete el.dataset.gatedTitle;
        }
      } else {
        el.setAttribute("aria-disabled", "true");
        el.classList.add("role-gated");
        if (el.tagName === "BUTTON" || el.tagName === "INPUT") {
          el.disabled = true;
        }
        // Preserve any pre-existing tooltip so we can restore it
        // if the user's role is upgraded mid-session.
        if (!el.dataset.gatedTitle) {
          el.dataset.gatedOriginalTitle = el.title || "";
          el.dataset.gatedTitle = "1";
        }
        var label = req.charAt(0).toUpperCase() + req.slice(1);
        el.title = t("$1 role required.", { role: label });
      }
    }
  }

  if (typeof document !== "undefined") {
    document.addEventListener("DOMContentLoaded", function () {
      applyI18n();
      gateByRole();
      // Reflect chosen locale in <html lang> for screen readers.
      try {
        document.documentElement.lang = _resolveLocale();
      } catch (_) {}
    });
  }

  // ─── Guided tour + contextual help (PR-57, audit D4) ─────────────
  //
  // The value reassessment found D4 (learning curve) had *fallen*
  // to 48 — five new tabs (graph, report, palette) without any
  // teaching material. This layer adds two things:
  //
  //   1. ``MnemosUI.startTour(steps)`` — a vanilla spotlight tour.
  //      Each step is {selector, title, body}; the engine dims the
  //      page, highlights the target, and shows a tooltip with
  //      Back / Next / Done. No library, no dependency.
  //   2. ``MnemosUI.helpButton(html)`` — a "?" button a page drops
  //      in its header; clicking it opens a modal with the page's
  //      contextual help.
  //
  // The dashboard fires a one-time welcome tour on first visit
  // (keyed off ``localStorage["mnemos_tour_done"]``).

  function _ensureTourEls() {
    var overlay = document.getElementById("tour-overlay");
    if (overlay) return overlay;
    overlay = document.createElement("div");
    overlay.id = "tour-overlay";
    overlay.className = "tour-overlay";
    overlay.innerHTML =
      '<div class="tour-spotlight" id="tour-spotlight"></div>'
      + '<div class="tour-pop" id="tour-pop" role="dialog" aria-modal="true">'
      + '<h3 id="tour-title"></h3>'
      + '<p id="tour-body"></p>'
      + '<div class="tour-nav">'
      + '<span id="tour-progress" class="muted"></span>'
      + '<span class="tour-nav-btns">'
      + '<button type="button" id="tour-skip">' + t("Skip") + '</button>'
      + '<button type="button" id="tour-back">' + t("Back") + '</button>'
      + '<button type="button" id="tour-next" class="primary">' + t("Next") + '</button>'
      + '</span></div></div>';
    document.body.appendChild(overlay);
    return overlay;
  }

  var _tourSteps = [];
  var _tourIdx = 0;

  function _renderTourStep() {
    var step = _tourSteps[_tourIdx];
    if (!step) return _endTour();
    var spotlight = document.getElementById("tour-spotlight");
    var pop = document.getElementById("tour-pop");
    var target = step.selector
      ? document.querySelector(step.selector)
      : null;
    document.getElementById("tour-title").textContent = step.title || "";
    document.getElementById("tour-body").textContent = step.body || "";
    document.getElementById("tour-progress").textContent =
      (_tourIdx + 1) + " / " + _tourSteps.length;
    document.getElementById("tour-back").disabled = _tourIdx === 0;
    document.getElementById("tour-next").textContent =
      _tourIdx === _tourSteps.length - 1 ? t("Done") : t("Next");
    if (target) {
      var r = target.getBoundingClientRect();
      spotlight.style.display = "block";
      spotlight.style.top = (r.top - 6) + "px";
      spotlight.style.left = (r.left - 6) + "px";
      spotlight.style.width = (r.width + 12) + "px";
      spotlight.style.height = (r.height + 12) + "px";
      // Place the popover below the target, or above if no room.
      var below = r.bottom + 12;
      pop.style.top = (below + 160 > window.innerHeight ? r.top - 170 : below) + "px";
      pop.style.left = Math.max(12, Math.min(r.left, window.innerWidth - 320)) + "px";
    } else {
      spotlight.style.display = "none";
      pop.style.top = "30vh";
      pop.style.left = "calc(50vw - 160px)";
    }
  }

  function _endTour() {
    var overlay = document.getElementById("tour-overlay");
    if (overlay) overlay.remove();
    try { localStorage.setItem("mnemos_tour_done", "1"); } catch (_) {}
  }

  function startTour(steps) {
    if (!steps || !steps.length) return;
    _tourSteps = steps;
    _tourIdx = 0;
    var overlay = _ensureTourEls();
    overlay.style.display = "block";
    document.getElementById("tour-next").onclick = function () {
      if (_tourIdx >= _tourSteps.length - 1) return _endTour();
      _tourIdx += 1;
      _renderTourStep();
    };
    document.getElementById("tour-back").onclick = function () {
      if (_tourIdx > 0) { _tourIdx -= 1; _renderTourStep(); }
    };
    document.getElementById("tour-skip").onclick = _endTour;
    _renderTourStep();
  }

  function tourDone() {
    try { return localStorage.getItem("mnemos_tour_done") === "1"; }
    catch (_) { return false; }
  }

  // Contextual help — a "?" button that opens a modal.
  function helpButton(titleKey, bodyHtml) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "help-btn";
    btn.setAttribute("aria-label", t("Help"));
    btn.textContent = "?";
    btn.addEventListener("click", function () {
      var modal = document.getElementById("help-modal");
      if (!modal) {
        modal = document.createElement("div");
        modal.id = "help-modal";
        modal.className = "help-modal";
        modal.innerHTML =
          '<div class="help-modal-box">'
          + '<header><h3 id="help-modal-title"></h3>'
          + '<button type="button" id="help-modal-close" aria-label="'
          + t("Close") + '">✕</button></header>'
          + '<div id="help-modal-body"></div></div>';
        document.body.appendChild(modal);
        modal.addEventListener("click", function (ev) {
          if (ev.target === modal
              || ev.target.id === "help-modal-close") {
            modal.style.display = "none";
          }
        });
      }
      document.getElementById("help-modal-title").textContent = t(titleKey);
      document.getElementById("help-modal-body").innerHTML = bodyHtml;
      modal.style.display = "flex";
    });
    return btn;
  }

  // ─── Voice status + output (PR-155 / PR-156) ─────────────────────
  //
  // ``MnemosUI.voiceStatus()`` caches one probe of /api/v1/voice/status so
  // both the mic (STT) and listen (TTS) buttons can decide whether to show
  // without each re-hitting the endpoint. Shape:
  //   { available, engine, model,           // STT, flat (back-compat)
  //     stt: {available, engine, model, language},
  //     tts: {available, engine, voice, lang_code} }
  var _voiceStatusPromise = null;
  function voiceStatus() {
    if (!_voiceStatusPromise) {
      _voiceStatusPromise = fetch("/api/v1/voice/status",
        { headers: { "accept": "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .catch(function () { return null; });
    }
    return _voiceStatusPromise;
  }

  // ``MnemosUI.speak(text, opts)`` reads text aloud via the local TTS
  // engine (Kokoro). Synthesis runs on the server; the returned audio/wav
  // Blob is played through an <audio> element (a blob: URL — allowed by the
  // CSP ``media-src 'self' blob:``). Returns a promise resolving to whether
  // playback started.
  var _ttsAudio = null;
  function speak(text, opts) {
    opts = opts || {};
    text = (text || "").trim();
    if (!text) return Promise.resolve(false);
    var body = { text: text };
    if (opts.voice) body.voice = opts.voice;
    return fetch("/api/v1/voice/speak", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      if (!r.ok) {
        if (r.status === 503) {
          showToast(t("Voice output isn't enabled on this server."), "error");
        } else {
          showError("Speech failed", r);
        }
        return false;
      }
      return r.blob().then(function (blob) {
        try {
          if (_ttsAudio) { try { _ttsAudio.pause(); } catch (_) {} }
          var url = URL.createObjectURL(blob);
          _ttsAudio = new Audio(url);
          _ttsAudio.addEventListener("ended", function () {
            URL.revokeObjectURL(url);
          });
          var p = _ttsAudio.play();
          if (p && p.catch) p.catch(function () {});
        } catch (_) { return false; }
        return true;
      });
    });
  }

  // ─── Voice command input (PR-155) ────────────────────────────────
  //
  // ``MnemosUI.mountVoiceInput({ button, target, projectInput })`` turns
  // a button into a push-to-talk control: click to record, click again
  // to stop. The clip is POSTed to ``/api/v1/voice/transcribe`` and the
  // recognised text is dropped into ``target``.
  //
  // The speech→text itself runs on the SERVER (a local faster-whisper
  // model), not in the browser — so no audio leaves the operator's own
  // deployment. The browser only captures the clip. That is why we use
  // MediaRecorder + a POST rather than the built-in ``SpeechRecognition``
  // API, which streams microphone audio to a cloud vendor.
  //
  // Degrades quietly: if the browser lacks MediaRecorder/getUserMedia
  // (e.g. a plain-http origin, which isn't a secure context) or the
  // server reports no STT engine installed, the button hides itself.
  function mountVoiceInput(opts) {
    opts = opts || {};
    var btn = typeof opts.button === "string"
      ? document.querySelector(opts.button) : opts.button;
    var target = typeof opts.target === "string"
      ? document.querySelector(opts.target) : opts.target;
    if (!btn || !target) return;

    function resolveEl(ref) {
      if (!ref) return null;
      return typeof ref === "string" ? document.querySelector(ref) : ref;
    }

    function hide() {
      btn.hidden = true;
      btn.setAttribute("aria-hidden", "true");
    }

    // getUserMedia + MediaRecorder only exist in a secure context
    // (https or localhost). Outside one they're undefined; hide rather
    // than throw on first click.
    var supported = !!(navigator.mediaDevices
      && navigator.mediaDevices.getUserMedia
      && typeof window.MediaRecorder !== "undefined");
    if (!supported) { hide(); return; }

    // Confirm the server actually has an STT engine before offering the mic.
    voiceStatus().then(function (s) {
      var stt = s && (s.stt || s);
      if (!stt || !stt.available) { hide(); return; }
      btn.hidden = false;
      btn.removeAttribute("aria-hidden");
      btn.title = t("Speak your question (local recognition: $1)",
        { m: stt.model || stt.engine });
    });

    var rec = null, chunks = [], recording = false, stream = null;

    function setRecording(on) {
      recording = on;
      btn.classList.toggle("recording", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      btn.setAttribute("aria-label",
        on ? t("Stop recording") : t("Start voice input"));
    }

    function stopStream() {
      if (stream) {
        stream.getTracks().forEach(function (tr) { tr.stop(); });
        stream = null;
      }
    }

    function start() {
      navigator.mediaDevices.getUserMedia({ audio: true }).then(function (s) {
        stream = s;
        chunks = [];
        try { rec = new MediaRecorder(stream); }
        catch (e) { rec = new MediaRecorder(stream, {}); }
        rec.ondataavailable = function (ev) {
          if (ev.data && ev.data.size) chunks.push(ev.data);
        };
        rec.onstop = function () { stopStream(); upload(); };
        rec.start();
        setRecording(true);
      }).catch(function () {
        showToast(t("Microphone access was blocked. Allow it in your "
          + "browser to use voice input."), "error");
      });
    }

    function stop() {
      if (rec && rec.state !== "inactive") rec.stop();
      setRecording(false);
    }

    function upload() {
      if (!chunks.length) return;
      var mime = (rec && rec.mimeType) || "audio/webm";
      var blob = new Blob(chunks, { type: mime });
      var ext = mime.indexOf("mp4") >= 0 ? "mp4"
        : (mime.indexOf("ogg") >= 0 ? "ogg" : "webm");
      var fd = new FormData();
      fd.append("audio", blob, "command." + ext);
      // Send the operator's chosen UI locale as a recognition hint —
      // a Korean operator's command is recognised as Korean, not guessed.
      var loc = "";
      try { loc = _resolveLocale(); } catch (_) {}
      if (loc) fd.append("language", loc);
      var pid = "";
      var pin = resolveEl(opts.projectInput);
      if (pin && pin.value) pid = pin.value.trim();
      else if (typeof currentProjectFromUrl === "function") {
        pid = currentProjectFromUrl() || "";
      }
      if (pid) fd.append("project_id", pid);

      btn.disabled = true;
      btn.classList.add("busy");
      // ``fetch`` is auto-patched to attach the CSRF token; FormData sets
      // its own multipart Content-Type, so we touch neither header.
      fetch("/api/v1/voice/transcribe", { method: "POST", body: fd })
        .then(function (r) {
          if (!r.ok) {
            if (r.status === 503) {
              showToast(t("Voice recognition isn't enabled on this server."),
                "error");
              hide();
              return null;
            }
            return showError("Transcription failed", r).then(function () {
              return null;
            });
          }
          return r.json();
        })
        .then(function (data) {
          if (!data) return;
          var text = (data.text || "").trim();
          if (!text) {
            showToast(t("Didn't catch that — please try again."), "warn");
            return;
          }
          // Append (don't clobber) so a second utterance extends the
          // first — natural for building up a longer question by voice.
          var cur = (target.value || "").trim();
          target.value = cur ? (cur + " " + text) : text;
          target.focus();
          try {
            target.dispatchEvent(new Event("input", { bubbles: true }));
          } catch (_) {}
          var shown = text.length > 60 ? text.slice(0, 60) + "…" : text;
          showToast(t("Recognised: $1", { text: shown }), "success");
          if (typeof opts.onResult === "function") opts.onResult(text, data);
        })
        .finally(function () {
          btn.disabled = false;
          btn.classList.remove("busy");
          btn.setAttribute("aria-label", t("Start voice input"));
        });
    }

    setRecording(false);
    btn.addEventListener("click", function () {
      if (recording) stop(); else start();
    });
  }

  window.MnemosUI = {
    showToast: showToast,
    startTour: startTour,
    tourDone: tourDone,
    helpButton: helpButton,
    showError: showError,
    renderJson: renderJson,
    renderJsonFromScript: renderJsonFromScript,
    copyToClipboard: copyToClipboard,
    showDialog: showDialog,
    closeDialog: closeDialog,
    escapeHtml: escapeHtml,
    bindRationaleCounter: bindRationaleCounter,
    clockOffsetFromPayload: clockOffsetFromPayload,
    relativeTime: relativeTime,
    hydrateRelativeTimes: hydrateRelativeTimes,
    publishSseState: publishSseState,
    t: t,
    setLocale: setLocale,
    applyI18n: applyI18n,
    gateByRole: gateByRole,
    icon: icon,
    exportCsv: exportCsv,
    exportXlsx: exportXlsx,
    renderMermaid: renderMermaid,
    mountProjectPicker: mountProjectPicker,
    mountVoiceInput: mountVoiceInput,
    voiceStatus: voiceStatus,
    speak: speak,
    currentProjectFromUrl: currentProjectFromUrl,
    currentProject: _currentProjectContext,
    notify: notify,
    readNotifications: readNotifications,
    clearNotifications: clearNotifications,
    mountCommentThread: mountCommentThread,
    rememberProject: rememberProject,
  };
  // Convenience global — many existing templates already call
  // ``escapeHtml(x)`` without a namespace prefix.
  if (typeof window.escapeHtml === "undefined") {
    window.escapeHtml = escapeHtml;
  }
})();
