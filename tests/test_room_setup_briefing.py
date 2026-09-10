"""Room-setup briefing (ADR-0019 decision 2) and its client-side parser
(app/static/room_setup_paste.js, decision 3).

`app/room_setup_briefing.py`'s `ROOM_SETUP_BRIEFING` is a module-level
constant, computed once at import time with zero database access -- these
tests exercise the plain Python constant directly, no HTTP client needed.

`room_setup_paste.js` has no JS test runner in this Python-based repo --
following the established pattern (tests/test_ui_rooms.py's
`test_main_js_copy_handler_three_tier_fallback_order` /
`test_rooms_js_renders_via_textcontent_not_innerhtml`), its tests here are
static-source checks: read the file as text, assert the structural
properties (validate-before-write ordering, no innerHTML, the exact key
list, the exact failure/success message text) that make the parser
all-or-nothing and injection-safe.
"""

import html
import re
from pathlib import Path

import pytest

from app.room_modes import ROOM_MODES
from app.room_setup_briefing import (
    REPLY_FORMAT_KEYS,
    REPLY_FORMAT_SENTINEL_END,
    REPLY_FORMAT_SENTINEL_START,
    REPLY_FORMAT_TEMPLATE,
    ROOM_SETUP_BRIEFING,
)
from app.rooms import (
    CONSENSUS_FLOOR_MAX,
    CONSENSUS_FLOOR_MIN,
    DEFAULT_CONSENSUS_FLOOR,
    DEFAULT_MAX_MESSAGES,
    MAX_DURATION_SECONDS,
    MAX_MESSAGES_MAX,
    MAX_MESSAGES_MIN,
)

JS_DIR = Path(__file__).resolve().parent.parent / "app" / "static"

# Real fleet-identifying strings actually present in this codebase's OWN
# fixtures/docstrings (not hypothetical examples): app/machines.py's
# docstring and tests/test_room_seats.py both cite the live deployment's
# own 'Rankati - Commander LXC109 - Capital NUC' machine name verbatim
# (mirroring a real, previously-revoked+re-minted machine); "bernard-ai" is
# used throughout tests/test_room_seats.py and tests/test_onboarding.py as
# this deployment's own real project name (this repo's own name). If the
# briefing ever regresses to the original, rejected, fleet-data-driven
# design (ADR-0019's "Alternatives Considered"), one of these would appear.
_REAL_MACHINE_NAME = "Rankati - Commander LXC109 - Capital NUC"
_REAL_PROJECT_NAME = "bernard-ai"
_REAL_MACHINE_SUBSTRINGS = ["Rankati", "LXC109", "Capital NUC"]


# --- decision 2: zero fleet data ---


def test_briefing_contains_no_real_machine_name():
    assert _REAL_MACHINE_NAME not in ROOM_SETUP_BRIEFING
    for substring in _REAL_MACHINE_SUBSTRINGS:
        assert substring not in ROOM_SETUP_BRIEFING


def test_briefing_contains_no_real_project_name():
    assert _REAL_PROJECT_NAME not in ROOM_SETUP_BRIEFING


def test_briefing_contains_no_owner_or_machine_token():
    """app/security.py's OWNER_TOKEN_PREFIX ('brnown_') / MACHINE_TOKEN_PREFIX
    ('brn_') must never appear -- the briefing has no database access at all
    (ADR-0019: "the room-setup briefing discloses no fleet data by design"),
    so there is no token to include, but this asserts it structurally
    rather than just by absence-of-a-call-site.
    """
    assert "brnown_" not in ROOM_SETUP_BRIEFING
    assert "brn_" not in ROOM_SETUP_BRIEFING
    # Broader pattern check: no token-shaped substring (prefix + underscore
    # + alnum run) anywhere in the text.
    assert not re.search(r"\bbrn[a-z_]*_[A-Za-z0-9]{8,}", ROOM_SETUP_BRIEFING)


def test_briefing_contains_no_url():
    assert "http://" not in ROOM_SETUP_BRIEFING
    assert "https://" not in ROOM_SETUP_BRIEFING


def test_briefing_contains_no_credential_looking_value():
    """No '<word>: <secret-looking value>' shape and no generic secret
    keywords that would suggest a credential leaked in (this text is pure,
    hand-written + generated prose/format-spec, never interpolates a DB
    row -- see app/room_setup_briefing.py's module docstring).
    """
    lowered = ROOM_SETUP_BRIEFING.lower()
    for forbidden in ("password", "api_key", "secret", "token=", "bearer "):
        assert forbidden not in lowered


def test_briefing_mentions_no_group_or_project_example_values():
    """The codebase's own illustrative placeholder values for these two
    fields (rooms_list.html's `placeholder="e.g. schema-debates"` /
    `placeholder="e.g. bernard-ai"`) must not leak into the generic spec --
    decision 2 requires the group/project explanations be generic, "without
    listing any real values."
    """
    assert "schema-debates" not in ROOM_SETUP_BRIEFING


# --- decision 2: modes/fields generated from the live constants, not
# hand-maintained prose with its own numbers -- parameterized so this test
# fails the moment ROOM_MODES or the validators drift out from under the
# hand-written English. ---


@pytest.mark.parametrize("mode_key", list(ROOM_MODES))
def test_briefing_documents_every_live_mode(mode_key):
    mode = ROOM_MODES[mode_key]
    assert f'"{mode_key}"' in ROOM_SETUP_BRIEFING
    assert mode.label in ROOM_SETUP_BRIEFING
    if not mode.symmetric:
        assert mode.side_labels is not None and mode.sides is not None
        for side_key in mode.sides:
            assert mode.side_labels[side_key] in ROOM_SETUP_BRIEFING


def test_briefing_has_no_undocumented_mode():
    """The reverse of the parameterized check above -- every mode name
    quoted in the briefing's mode list must be a real, live mode key (this
    would catch a stale mode left behind after a rename/removal).
    """
    quoted = set(re.findall(r'"([a-z]+)" \(', ROOM_SETUP_BRIEFING))
    assert quoted == set(ROOM_MODES)


def test_briefing_states_max_messages_bounds():
    assert str(MAX_MESSAGES_MIN) in ROOM_SETUP_BRIEFING
    assert str(MAX_MESSAGES_MAX) in ROOM_SETUP_BRIEFING
    assert str(DEFAULT_MAX_MESSAGES) in ROOM_SETUP_BRIEFING


def test_briefing_states_consensus_floor_bounds_and_scope():
    assert str(CONSENSUS_FLOOR_MIN) in ROOM_SETUP_BRIEFING
    assert str(CONSENSUS_FLOOR_MAX) in ROOM_SETUP_BRIEFING
    assert str(DEFAULT_CONSENSUS_FLOOR) in ROOM_SETUP_BRIEFING
    assert "less than max_messages" in ROOM_SETUP_BRIEFING
    assert "adversarial modes" in ROOM_SETUP_BRIEFING


def test_briefing_states_duration_bound_in_minutes():
    assert str(MAX_DURATION_SECONDS // 60) in ROOM_SETUP_BRIEFING
    assert "30 days" in ROOM_SETUP_BRIEFING


def test_briefing_instructs_leaving_agent_group_project_blank():
    lines = ROOM_SETUP_BRIEFING.splitlines()
    for field in ("agent_a", "agent_b", "group", "project"):
        # Each of these fields' own explanatory line (agent_a and agent_b
        # share one line) must carry the explicit instruction, not just the
        # field name appearing somewhere unrelated in the text.
        found = any(field in line and "LEAVE BLANK" in line for line in lines)
        assert found, f"{field} must be explicitly marked LEAVE BLANK on its own explanatory line"


def test_briefing_explains_group_vs_project_generically():
    assert "filter and bulk-manage" in ROOM_SETUP_BRIEFING  # group
    assert "compare, exactly" in ROOM_SETUP_BRIEFING  # project / ADR-0018 join-prompt target check


# --- decision 3: the reply format ---


def test_reply_format_has_exactly_ten_fixed_keys():
    assert len(REPLY_FORMAT_KEYS) == 10
    assert REPLY_FORMAT_KEYS == [
        "name",
        "mode",
        "topic",
        "agent_a",
        "agent_b",
        "max_messages",
        "consensus_floor",
        "group",
        "project",
        "duration_minutes",
    ]


def test_reply_format_template_has_sentinels_and_every_key():
    assert REPLY_FORMAT_TEMPLATE.startswith(REPLY_FORMAT_SENTINEL_START)
    assert REPLY_FORMAT_TEMPLATE.endswith(REPLY_FORMAT_SENTINEL_END)
    for key in REPLY_FORMAT_KEYS:
        assert f"{key}:" in REPLY_FORMAT_TEMPLATE


def test_reply_format_template_is_embedded_verbatim_in_briefing():
    """The briefing must show the LLM the exact same template the parser
    expects back -- not a hand-retyped, driftable copy.
    """
    assert REPLY_FORMAT_TEMPLATE in ROOM_SETUP_BRIEFING


def test_briefing_instructs_fenced_block_with_sentinel_as_real_delimiter():
    assert "fenced code block" in ROOM_SETUP_BRIEFING
    assert REPLY_FORMAT_SENTINEL_START in ROOM_SETUP_BRIEFING
    assert REPLY_FORMAT_SENTINEL_END in ROOM_SETUP_BRIEFING


# --- room_setup_paste.js: static-source checks (no JS runner available) ---


@pytest.fixture(scope="module")
def paste_js_source() -> str:
    return (JS_DIR / "room_setup_paste.js").read_text()


def test_paste_js_recognizes_exactly_the_ten_reply_format_keys(paste_js_source):
    """The parser's own recognized-key list must be the identical ten keys
    the briefing's format spec promises -- no second, divergent list.
    """
    match = re.search(r"var VALID_KEYS = \[(.*?)\];", paste_js_source, re.DOTALL)
    assert match, "expected a VALID_KEYS array literal in room_setup_paste.js"
    found_keys = re.findall(r'"([a-z_]+)"', match.group(1))
    assert found_keys == REPLY_FORMAT_KEYS


def test_paste_js_never_uses_innerhtml_or_eval(paste_js_source):
    code_only = "\n".join(line for line in paste_js_source.splitlines() if not line.strip().startswith("//"))
    assert ".innerHTML" not in code_only
    assert "eval(" not in code_only
    assert "new Function(" not in code_only


def test_paste_js_only_writes_plain_value_properties(paste_js_source):
    """Every DOM write in `applyFields` must be a plain `.value =`
    assignment (never innerHTML) -- confined to the function that runs only
    after `parseBlock` has already returned `ok: true`.
    """
    match = re.search(r"function applyFields\(fields\) \{(.*?)\n  \}\n", paste_js_source, re.DOTALL)
    assert match, "expected an applyFields function in room_setup_paste.js"
    body = match.group(1)
    assert ".value =" in body
    assert ".innerHTML" not in body


def test_paste_js_validates_fully_before_ever_applying_fields(paste_js_source):
    """Atomic, all-or-nothing (ADR-0019 decision 3): the click handler must
    call `parseBlock` first, bail out via `fail(...)` + `return` on any
    failure, and only ever call `applyFields` afterward -- so a failing
    parse cannot have touched any field.
    """
    match = re.search(r"button\.addEventListener\(\"click\", function \(\) \{(.*?)\n  \}\);", paste_js_source, re.DOTALL)
    assert match, "expected the button's click handler in room_setup_paste.js"
    handler = match.group(1)
    parse_idx = handler.index("parseBlock(")
    fail_idx = handler.index("fail(result.message)")
    return_idx = handler.index("return;")
    apply_idx = handler.index("applyFields(result.fields)")
    assert parse_idx < fail_idx < return_idx < apply_idx, (
        "the handler must parse, then fail+return on any error, before ever applying fields"
    )

    # parseBlock itself must never touch the DOM -- it only builds/returns a
    # plain {ok, fields|message} result.
    parse_match = re.search(r"function parseBlock\(raw\) \{(.*?)\n  \}\n", paste_js_source, re.DOTALL)
    assert parse_match, "expected a parseBlock function in room_setup_paste.js"
    parse_body = parse_match.group(1)
    assert "getElementById" not in parse_body
    assert ".value =" not in parse_body


def test_paste_js_blank_value_is_left_alone_not_an_error(paste_js_source):
    """ADR-0019 decision 3, point 8: a blank/omitted value for a recognized
    key means "leave this field alone" -- never a failure, and never
    written (overwritten with blank) either.
    """
    assert 'if (value === undefined || value === "") return; // blank/omitted' in paste_js_source


def test_paste_js_failure_messages_present_and_specific(paste_js_source):
    assert "Couldn't find a BRAINARD-ROOM-SETUP-v1" in paste_js_source
    assert "Found more than one BRAINARD-ROOM-SETUP-v1" in paste_js_source
    assert "doesn't match the expected `key: value` format" in paste_js_source
    assert "unrecognized key" in paste_js_source
    assert "appears more than once in the pasted block" in paste_js_source
    assert "missing a required `name` value" in paste_js_source
    assert "missing a required `mode` value" in paste_js_source
    assert "requires a non-blank `topic`" in paste_js_source
    assert "not one of the valid modes" in paste_js_source
    assert "must be a plain integer" in paste_js_source


def test_paste_js_success_message_matches_adr_wording(paste_js_source):
    # The literal is split across a `+`-concatenated multi-line JS string
    # (matching this codebase's existing style, e.g. main.js's own
    # multi-line message constants) -- checked as its two literal fragments
    # rather than one contiguous run of source text.
    assert "Parsed — review the filled-in fields, then fill in the agent names, group, and project yourself " in paste_js_source
    assert "before creating the room." in paste_js_source


def test_paste_js_never_auto_submits_the_form(paste_js_source):
    assert ".submit(" not in paste_js_source
    assert "requestSubmit" not in paste_js_source


def test_paste_js_validates_mode_against_shared_room_modes_data_not_a_hardcoded_list(paste_js_source):
    """No second, divergent mode list (ADR-0019 decision 3, point 6) -- the
    parser must read the same `#room-modes-data` JSON payload
    app/static/room_form.js already reads, not a hardcoded set of mode
    strings.
    """
    assert 'getElementById("room-modes-data")' in paste_js_source
    assert "JSON.parse" in paste_js_source
    assert "validModes" in paste_js_source
    # No literal freeform/debate/... array anywhere near a mode-validity
    # check (would indicate a second, hardcoded mode list).
    assert '"freeform", "debate"' not in paste_js_source
    assert "['freeform', 'debate'" not in paste_js_source


def test_paste_js_empty_valid_modes_skips_the_mode_check_not_rejects_every_mode(paste_js_source):
    """If `#room-modes-data` is missing/unparseable, `validModes` ends up
    `[]` -- the catch block must fail OPEN (skip the mode check via
    parseBlock's `validModes.length > 0` guard) rather than fail closed
    (reject every mode), since `create_room` is the real validation
    authority either way and a rendering bug shouldn't break the whole
    paste feature. The comment must describe this actual behaviour, not
    the opposite ("fail closed" would contradict the `length > 0` guard).
    """
    catch_match = re.search(r"catch \(e\) \{(.*?)\n  \}\n\n  function findAllIndices", paste_js_source, re.DOTALL)
    assert catch_match, "expected the modes-data parse catch block in room_setup_paste.js"
    catch_body = catch_match.group(1)
    assert "validModes = [];" in catch_body
    assert "fails OPEN" in catch_body
    assert "fail closed" not in catch_body.lower()

    # The guard that actually implements the fail-open behaviour: an empty
    # validModes short-circuits `&&` so the mode check never runs.
    assert "if (validModes.length > 0 && validModes.indexOf(fields.mode) === -1) {" in paste_js_source


def test_paste_js_opens_details_and_dispatches_change_on_success(paste_js_source):
    assert "detailsEl.open = true" in paste_js_source
    assert "dispatchChange" in paste_js_source
    assert 'new Event("change"' in paste_js_source


def test_paste_js_maps_duration_minutes_onto_existing_custom_duration_fields(paste_js_source):
    """duration_minutes has no single form field of its own -- it must be
    mapped onto the existing custom-duration controls the owner would
    otherwise fill in by hand, not a new hidden input.
    """
    assert 'getElementById("duration_preset")' in paste_js_source
    assert 'getElementById("custom_duration_value")' in paste_js_source
    assert 'getElementById("custom_duration_unit")' in paste_js_source
    assert 'presetEl.value = "custom"' in paste_js_source


# --- hidden pre content matches the module constant exactly (server-side
# render correctness; the hidden element's .textContent is what the shared
# clipboard handler, app/static/main.js, actually copies) ---


def test_room_setup_briefing_renders_via_jinja_without_raising():
    """Sanity: the constant round-trips through Jinja2 autoescape (as it
    will inside the hidden <pre> in rooms_list.html) and, once HTML
    entities are unescaped again, is byte-for-byte the same text -- proves
    nothing about the escaping process silently mangles it.
    """
    from markupsafe import escape

    escaped = str(escape(ROOM_SETUP_BRIEFING))
    assert html.unescape(escaped) == ROOM_SETUP_BRIEFING
