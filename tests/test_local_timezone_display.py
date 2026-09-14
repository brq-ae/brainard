"""Owner-facing local-timezone display (display-only fix).

The container runs entirely in UTC; the owner reads the UI from a fixed
offset of his own. `app/templates_env.py`'s `human_ts` filter now wraps
every rendered timestamp in a `<time datetime="...">` element carrying the
exact UTC instant, and `app/static/localtime.js` rewrites each such
element's visible text to the viewer's local time on page load (and, for
room messages appended live by `app/static/rooms.js`'s poll loop, on
arrival too). This is deliberately NOT wired into storage, API responses,
join prompts, bootstraps, exports, deposits, or notifications -- those stay
plain UTC, unchanged; several tests below assert that explicitly, since
it's the part most likely to regress by accident.

No JS runtime is available in this Python-based test suite -- JS
correctness is checked via static-source assertions on the .js files
themselves, following the established pattern (see
tests/test_room_setup_briefing.py's module docstring and
tests/test_ui_rooms.py's test_main_js_copy_handler_three_tier_fallback_order
/ test_rooms_js_renders_via_textcontent_not_innerhtml).
"""

import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from markupsafe import Markup

from app.templates_env import _human_ts_filter, templates

JS_DIR = Path(__file__).resolve().parent.parent / "app" / "static"
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "app" / "templates"


# --- _human_ts_filter: the server side of the conversion ---


def test_human_ts_filter_none_renders_em_dash():
    result = _human_ts_filter(None)
    assert result == "—"


def test_human_ts_filter_returns_markup_not_plain_str():
    """Must be Markup (pre-escaped), or Jinja2 autoescape would HTML-escape
    the <time> tag itself into literal `&lt;time&gt;` text on render.
    """
    result = _human_ts_filter(datetime(2026, 8, 6, 14, 32, tzinfo=UTC))
    assert isinstance(result, Markup)


def test_human_ts_filter_emits_time_element_with_iso_datetime_and_marker():
    value = datetime(2026, 8, 6, 14, 32, tzinfo=UTC)
    result = _human_ts_filter(value)
    assert result == '<time datetime="2026-08-06T14:32:00+00:00" data-local-ts>2026-08-06 14:32 UTC</time>'


def test_human_ts_filter_fallback_text_matches_prior_utc_rendering():
    """The element's own text -- what a no-JS browser (or one where
    localtime.js hasn't run yet) actually shows -- must be byte-for-byte
    the same 'YYYY-MM-DD HH:MM UTC' rendering this filter has always
    produced, never blank and never a raw ISO string.
    """
    value = datetime(2026, 8, 6, 14, 32, tzinfo=UTC)
    result = _human_ts_filter(value)
    assert ">2026-08-06 14:32 UTC</time>" in result


def test_human_ts_filter_naive_datetime_treated_as_utc():
    naive = datetime(2026, 8, 6, 14, 32)  # no tzinfo, as stored values sometimes are
    result = _human_ts_filter(naive)
    assert "2026-08-06T14:32:00+00:00" in result
    assert "2026-08-06 14:32 UTC" in result


def test_human_ts_filter_converts_non_utc_aware_datetime_to_utc():
    tz_plus_4 = timezone(timedelta(hours=4))
    value = datetime(2026, 8, 6, 18, 32, tzinfo=tz_plus_4)  # 14:32 UTC
    result = _human_ts_filter(value)
    assert "2026-08-06T14:32:00+00:00" in result
    assert "2026-08-06 14:32 UTC" in result


def test_human_ts_filter_registered_on_template_env():
    assert templates.env.filters["human_ts"] is _human_ts_filter


# --- template rendering: the filter's HTML actually reaches the page unescaped ---


def test_human_ts_filter_renders_unescaped_through_jinja():
    """Round-trips through the actual Jinja2 environment (autoescape ON,
    same as every real page) to prove the <time> tag survives -- not just
    that the Python function returns the right Markup object.
    """
    template = templates.env.from_string("<span>{{ ts|human_ts }}</span>")
    rendered = template.render(ts=datetime(2026, 8, 6, 14, 32, tzinfo=UTC))
    assert rendered == '<span><time datetime="2026-08-06T14:32:00+00:00" data-local-ts>2026-08-06 14:32 UTC</time></span>'


def test_human_ts_filter_none_still_escapes_normally_in_template():
    template = templates.env.from_string("<span>{{ ts|human_ts }}</span>")
    rendered = template.render(ts=None)
    assert rendered == "<span>—</span>"


# --- base.html wires localtime.js on every page ---


def test_base_html_loads_localtime_js_before_main_js():
    source = (TEMPLATES_DIR / "base.html").read_text()
    localtime_idx = source.index('<script src="/static/localtime.js">')
    main_idx = source.index('<script src="/static/main.js">')
    assert localtime_idx < main_idx


# --- every template that renders a timestamp still uses the shared filter
# (a regression guard: this centralizes local-time conversion in the one
# filter rather than depending on each template individually, so this test
# asserts the filter is still the single call site for all of them) ---


_EXPECTED_HUMAN_TS_TEMPLATES = {
    "doctrine.html",
    "admin_machines.html",
    "rooms_list.html",
    "dashboard.html",
    "projects_list.html",
    "journal.html",
    "document_view.html",
    "admin_proposals.html",
    "library_list.html",
    "notifications.html",
    "library_entry.html",
    "project_detail.html",
    "librarian.html",
    "llm.html",
    "room_view.html",
}


@pytest.mark.parametrize("template_name", sorted(_EXPECTED_HUMAN_TS_TEMPLATES))
def test_template_still_uses_human_ts_filter(template_name):
    source = (TEMPLATES_DIR / template_name).read_text()
    assert "|human_ts" in source, f"{template_name} must still render its timestamp(s) via the human_ts filter"


def test_no_template_hand_formats_a_displayed_timestamp():
    """Nothing in app/templates/ should hand-format a *displayed* timestamp
    itself (that would skip the <time>/data-local-ts wrapping and silently
    stay UTC-only) -- every rendered timestamp must go through the one
    shared filter. room_view.html's `data-expires-at="{{
    room.expires_at.isoformat() ... }}"` is the one legitimate exception:
    it's a machine-readable data attribute feeding rooms.js's countdown
    timer (ADR-0007), not owner-facing display text, so it's out of this
    change's scope (see the task's point 1: only what the owner *reads*).
    """
    for path in TEMPLATES_DIR.glob("*.html"):
        source = path.read_text()
        assert ".strftime(" not in source, f"{path.name} must not hand-format timestamps"
        for line in source.splitlines():
            if ".isoformat()" in line:
                assert "data-expires-at" in line, f"{path.name}: unexpected raw .isoformat() display usage: {line}"


# --- room_view.html: a representative full-page render (not just the
# isolated filter) showing the <time> element actually lands in the
# transcript row ---


def test_room_view_template_renders_message_timestamp_via_time_element():
    """Reuses this suite's own established room_view.html render helpers
    (tests/test_ui_rooms.py's `_fake_room` / `_render_room_view`, the same
    ones ADR-0013's join-prompt-loop tests use) rather than hand-building a
    fake context -- proves the <time> element actually lands in a real
    rendered transcript row, not just in the isolated filter call.
    """
    from app.models import RoomMessage
    from tests.test_ui_rooms import _fake_room, _render_room_view

    room = _fake_room()
    message = RoomMessage(
        id="msg-tz-fake",
        room_id=room.id,
        seq=1,
        sender="agent-a",
        text="hello",
        kind="message",
        created_at=datetime(2026, 8, 6, 14, 32, tzinfo=UTC),
        deleted_at=None,
    )
    rendered = _render_room_view(
        room=room,
        members=["agent-a", "agent-b"],
        sides={},
        side_labels={},
        join_prompts={"agent-a": "prompt-a", "agent-b": "prompt-b"},
        messages=[message],
    )
    assert '<time datetime="2026-08-06T14:32:00+00:00" data-local-ts>2026-08-06 14:32 UTC</time>' in rendered


# --- app/static/localtime.js: static-source checks ---


@pytest.fixture(scope="module")
def localtime_js_source() -> str:
    return (JS_DIR / "localtime.js").read_text()


def test_localtime_js_uses_textcontent_not_innerhtml(localtime_js_source):
    code_only = "\n".join(line for line in localtime_js_source.splitlines() if not line.strip().startswith("//"))
    assert ".innerHTML" not in code_only
    assert "textContent" in localtime_js_source


def test_localtime_js_uses_no_date_library_only_builtins(localtime_js_source):
    """No new dependency: built-in Date/Intl only, no <script src> to any
    CDN or bundled date library, no import/require statement.
    """
    assert "import " not in localtime_js_source
    assert "require(" not in localtime_js_source
    assert "moment" not in localtime_js_source.lower()
    assert "luxon" not in localtime_js_source.lower()
    assert "new Date(" in localtime_js_source


def test_localtime_js_selects_elements_by_data_local_ts_marker(localtime_js_source):
    assert 'querySelectorAll("time[data-local-ts]")' in localtime_js_source


def test_localtime_js_guards_against_missing_or_unparseable_datetime(localtime_js_source):
    """Graceful degradation: a missing/invalid `datetime` attribute must
    leave the element exactly as server-rendered (the existing UTC text),
    never throw, never blank it.
    """
    match = re.search(r"function convertOne\(timeEl\) \{(.*?)\n  \}\n", localtime_js_source, re.DOTALL)
    assert match, "expected a convertOne function in localtime.js"
    body = match.group(1)
    assert "if (!iso) return;" in body
    assert "isNaN(date.getTime())" in body


def test_localtime_js_format_includes_signed_utc_offset(localtime_js_source):
    """Requirement: the local rendering must make clear it is local, not
    UTC, so a stray UTC value elsewhere is never mistaken for one -- the
    chosen format is 'YYYY-MM-DD HH:MM UTC<sign><offset>'.
    """
    assert '"UTC" + sign' in localtime_js_source


def test_localtime_js_exposes_global_brain_time_namespace(localtime_js_source):
    assert "window.BrainTime = { convertOne: convertOne, convertAll: convertAll, formatLocal: formatLocal };" in (
        localtime_js_source
    )


def test_localtime_js_converts_on_load(localtime_js_source):
    assert "convertAll(document);" in localtime_js_source


# --- app/static/rooms.js: dynamically-appended messages get the same treatment ---


@pytest.fixture(scope="module")
def rooms_js_source() -> str:
    return (JS_DIR / "rooms.js").read_text()


def test_rooms_js_wraps_created_at_in_time_element(rooms_js_source):
    match = re.search(r"function appendMessage\(m\) \{(.*?)\n  \}\n", rooms_js_source, re.DOTALL)
    assert match, "expected an appendMessage function in rooms.js"
    body = match.group(1)
    assert 'document.createElement("time")' in body
    assert 'timeEl.setAttribute("datetime", m.created_at)' in body


def test_rooms_js_converts_appended_timestamps_via_shared_brain_time_helper(rooms_js_source):
    """Room messages arrive via polling and are inserted by JS -- must use
    the same window.BrainTime.convertOne conversion the server-rendered
    rows get (via localtime.js), guarded so a missing/not-yet-loaded helper
    degrades to the plain fallback text rather than throwing.
    """
    assert "window.BrainTime && window.BrainTime.convertOne" in rooms_js_source
    assert "window.BrainTime.convertOne(timeEl)" in rooms_js_source


def test_rooms_js_still_never_uses_innerhtml(rooms_js_source):
    """Regression guard on the pre-existing textContent/innerHTML
    discipline this change must not weaken (see
    test_rooms_js_renders_via_textcontent_not_innerhtml in test_ui_rooms.py).
    """
    code_only = "\n".join(line for line in rooms_js_source.splitlines() if not line.strip().startswith("//"))
    assert ".innerHTML" not in code_only


# --- explicit regression guards: agent-facing / API / export surfaces must
# still be plain UTC, completely untouched by this change ---


def test_onboarding_deadline_line_still_plain_utc_strftime_not_the_html_filter():
    """app/onboarding.py's join-prompt deadline line builds its own plain
    'YYYY-MM-DD HH:MM UTC' text independently of templates_env's human_ts
    filter -- it must never emit the <time>/data-local-ts markup (that
    would leak HTML into a plain-text prompt handed to an LLM).
    """
    source = Path(__file__).resolve().parent.parent.joinpath("app", "onboarding.py").read_text()
    assert 'strftime("%Y-%m-%d %H:%M UTC")' in source
    assert "data-local-ts" not in source
    assert "human_ts" not in source


def test_export_module_still_uses_raw_isoformat_not_the_html_filter():
    source = Path(__file__).resolve().parent.parent.joinpath("app", "routers", "export.py").read_text()
    assert "data-local-ts" not in source
    assert "human_ts" not in source


def test_bootstrap_module_still_uses_raw_isoformat_not_the_html_filter():
    source = Path(__file__).resolve().parent.parent.joinpath("app", "routers", "bootstrap.py").read_text()
    assert "data-local-ts" not in source
    assert "human_ts" not in source


def test_ui_rooms_poll_endpoint_still_returns_raw_isoformat_not_the_html_filter():
    """The JSON short-poll endpoint app/static/rooms.js fetches from
    (`/ui/rooms/{id}/messages`) must keep returning a plain ISO string in
    `created_at` -- conversion to local time happens entirely client-side,
    in rooms.js, never on this response.
    """
    source = Path(__file__).resolve().parent.parent.joinpath("app", "routers", "ui_rooms.py").read_text()
    assert '"created_at": m.created_at.isoformat(),' in source
    assert "data-local-ts" not in source


def test_human_ts_filter_module_docstring_states_scope():
    source = Path(__file__).resolve().parent.parent.joinpath("app", "templates_env.py").read_text()
    assert "Storage/API/agent-facing output is completely untouched" in source
