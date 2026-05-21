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
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename || ("mnemos-export-" + Date.now() + ".csv");
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
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
      var kindBadge = e.kind === "project" ? '<span class="cmdk-kind">project</span>' : "";
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

  function _onPaletteInput(ev) {
    var overlay = _paletteEl();
    if (!overlay || !overlay._entries) return;
    var filtered = _filterPaletteEntries(overlay._entries, ev.target.value);
    overlay._filtered = filtered;
    overlay._selected = 0;
    _renderPaletteResults(filtered, 0);
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
    "r": "/diffs", "f": "/findings", "s": "/settings",
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

  window._mnemosEditComment = async function (commentId, kind, targetId) {
    var fresh = prompt("Edit comment:");
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
    "Live analysis stream disconnected after 6 retries. Click the Monitor button to start a fresh stream, or reload the page to start over.":
      "분석 스트림 연결이 6회 재시도 후 끊어졌습니다. Monitor 버튼을 다시 누르거나 페이지를 새로고침하세요.",
    // Onboarding card
    "Welcome to Mnemos": "Mnemos에 오신 것을 환영합니다",
    "Register a GitLab project": "GitLab 프로젝트 등록",
    "Run the first analysis": "첫 분석 실행",
    "Review the results": "결과 검토",
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
    "Exported.": "내보냈습니다.",
    "Nothing to export.": "내보낼 항목이 없습니다.",
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
    "Generated by Mnemos — knowledge production platform.":
      "Mnemos — 지식 생산 플랫폼에서 생성됨.",
    "Enter a project ID and click Generate.": "프로젝트 ID 를 입력하고 생성을 클릭하세요.",
    "No L3 summary yet — run a full analysis to generate the system-level narrative.":
      "L3 요약이 아직 없습니다 — 전체 분석을 실행하여 시스템 수준 서술을 생성하세요.",
    // PR-52 — finding → plan.
    "Create plan": "계획 생성",
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
    "Sign in with SSO": "SSO 로 로그인",
    "Loaded shared approval context — paste the token to approve.":
      "공유된 승인 컨텍스트가 로드되었습니다 — 토큰을 붙여넣어 승인하세요.",
    // SSE strip
    "Analysis stream disconnected — open the Analysis tab to reconnect.":
      "분석 스트림 연결 끊김 — Analysis 탭을 열어 재연결하세요.",
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

  if (typeof document !== "undefined") {
    document.addEventListener("DOMContentLoaded", function () {
      applyI18n();
      // Reflect chosen locale in <html lang> for screen readers.
      try {
        document.documentElement.lang = _resolveLocale();
      } catch (_) {}
    });
  }

  window.MnemosUI = {
    showToast: showToast,
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
    icon: icon,
    exportCsv: exportCsv,
    notify: notify,
    readNotifications: readNotifications,
    clearNotifications: clearNotifications,
    mountCommentThread: mountCommentThread,
  };
  // Convenience global — many existing templates already call
  // ``escapeHtml(x)`` without a namespace prefix.
  if (typeof window.escapeHtml === "undefined") {
    window.escapeHtml = escapeHtml;
  }
})();
