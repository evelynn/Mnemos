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
    var ch = _bc();
    if (!ch) return;
    try { ch.postMessage({ state: state, at: Date.now() }); } catch (_) {}
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
      var ch = _bc();
      if (!ch) return;
      ch.onmessage = function (ev) {
        if (!ev || !ev.data) return;
        _applySseStrip(ev.data.state);
      };
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
  };
  // Convenience global — many existing templates already call
  // ``escapeHtml(x)`` without a namespace prefix.
  if (typeof window.escapeHtml === "undefined") {
    window.escapeHtml = escapeHtml;
  }
})();
