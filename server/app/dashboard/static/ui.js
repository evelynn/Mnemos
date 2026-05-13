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
    // PR-41 — notification centre + responsive.
    "Notifications": "알림",
    "Clear all": "모두 지우기",
    "No notifications yet.": "알림이 없습니다.",
    "Analysis run failed": "분석 실행 실패",
    "Toggle navigation": "메뉴 열기/닫기",
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
    notify: notify,
    readNotifications: readNotifications,
    clearNotifications: clearNotifications,
  };
  // Convenience global — many existing templates already call
  // ``escapeHtml(x)`` without a namespace prefix.
  if (typeof window.escapeHtml === "undefined") {
    window.escapeHtml = escapeHtml;
  }
})();
