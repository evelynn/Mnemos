"""PR-150 — the Ask (Q&A) GUI tab.

The /ask and /trace_flow capabilities existed only as REST endpoints with
no GUI surface — powerful but undiscoverable. This adds an "Ask" tab so an
operator can ask in plain language and see the answer (matched symbol,
signature, summary, data access) plus a "deepened" badge when Mnemos had
to analyse on demand. These guards keep the tab wired and renderable.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr150")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"


def test_ask_is_a_valid_tab_and_in_nav():
    import inspect

    from app.dashboard import router as dash

    src = inspect.getsource(dash.tab_page)
    assert '"ask"' in src, "ask must be an allowed tab"
    nav = (_TPL / "_layout.html").read_text(encoding="utf-8")
    assert 'href="/ask"' in nav, "Ask must be in the sidebar nav"


def test_ask_template_wires_endpoint_and_picker():
    html = (_TPL / "ask.html").read_text(encoding="utf-8")
    # PR-186: the Ask is now LLM-backed (graph-grounded /chat) + conversational.
    assert "/chat" in html and "renderChatReply" in html
    # Project selection moved to the global topbar picker (_layout); the Ask
    # reads it via currentProject — no raw per-page UUID box.
    assert "currentProject" in html
    # the cross-tier trace + on-demand source affordances remain usable
    assert "trace_flow/auto" in html


def test_ask_tab_can_trigger_process_trace():
    # PR-151/186 — the Ask tab triggers trace_flow/auto and renders the flow
    # (tier steps), so the cross-tier process feature is usable from the GUI.
    html = (_TPL / "ask.html").read_text(encoding="utf-8")
    assert "trace_flow/auto" in html
    assert "renderFlowHTML" in html
    assert "flow-step" in html
    assert 'id="aq-trace"' in html


def test_ask_template_renders_without_jinja_error():
    # Mirror PR-129's render guard for the new template specifically.
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(_TPL)),
        autoescape=select_autoescape(["html"]),
    )
    # _layout.html references request/user/active_tab + url helpers; render
    # the child block in isolation by compiling (parse) — catches syntax
    # errors without needing the full app context.
    env.parse((_TPL / "ask.html").read_text(encoding="utf-8"))
