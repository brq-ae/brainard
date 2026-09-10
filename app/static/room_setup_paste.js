// The Brain -- "paste setup" box for the room-setup briefing reply format
// (ADR-0019 decision 3). Vanilla JS, no framework, no CDN. No inline
// event-handler attributes (CSP-friendly, matches app/static/main.js).
//
// Parses a sentinel-delimited, `key: value` block (app/room_setup_briefing.py's
// REPLY_FORMAT_TEMPLATE) the owner pastes back from an external LLM
// conversation, and fills in the existing create-room form fields
// (app/templates/rooms_list.html) -- no server round trip, no new endpoint.
// `create_room` (app/rooms.py) remains the sole validation authority: this
// parser only resolves the pasted block's *shape* into the same plain form
// fields the owner would otherwise type into by hand, exactly the "UI layer
// resolves shape, the one domain function validates" split
// app/routers/ui_rooms.py already documents for `_sides_for_mode`/
// `_parse_duration_seconds` -- carried out here in the browser instead.
//
// Parsing is ALL-OR-NOTHING: every check below runs against a temporary
// dict before anything is written to the DOM. On any failure, no field is
// touched and a clear message names what was wrong. A blank/omitted value
// for a recognized key is never an error -- it means "leave that field
// exactly as it is" (decision 3, point 8).
//
// SECURITY: every value below is written only to a plain form control's
// `.value` property -- never `innerHTML`, never `eval`/`Function`, never
// posted anywhere by this script. The pasted text is owner-supplied but
// still treated as untrusted for DOM purposes: a hostile paste can, at
// worst, populate the visible form with attacker-chosen text the owner
// must still notice and deliberately submit through create_room's
// completely unchanged, full server-side validation (ADR-0019
// Consequences: "the identical trust boundary that already exists for
// anything the owner types into this form by hand today").
(function () {
  "use strict";

  var SENTINEL_START = "BRAINARD-ROOM-SETUP-v1";
  var SENTINEL_END = "END-BRAINARD-ROOM-SETUP";

  // The ten fixed keys (app/room_setup_briefing.py's REPLY_FORMAT_KEYS) --
  // kept as a literal list here (not fetched) since this file has no
  // server round trip at all; app/room_setup_briefing.py is the single
  // source of the *text* format, this is its client-side parser.
  var VALID_KEYS = [
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
  ];
  var INTEGER_KEYS = ["max_messages", "consensus_floor", "duration_minutes"];
  var KEY_PATTERN = /^[A-Za-z0-9_]+$/;
  var INTEGER_PATTERN = /^-?[0-9]+$/;

  // Plain form-field targets -- one id per key, except duration_minutes
  // (mapped onto the existing custom-duration fields below, since there is
  // no single "duration" input on the form).
  var FIELD_TARGET_IDS = {
    name: "name",
    mode: "mode",
    topic: "topic",
    agent_a: "agent_a",
    agent_b: "agent_b",
    max_messages: "max_messages",
    consensus_floor: "consensus_floor",
    group: "group",
    project: "project",
  };

  var NOT_FOUND_MESSAGE =
    "Couldn't find a BRAINARD-ROOM-SETUP-v1 … END-BRAINARD-ROOM-SETUP block in the pasted text. Paste " +
    "the LLM's exact reply to the copied briefing, unedited -- plain prose or a differently-shaped answer " +
    "won't parse.";
  var AMBIGUOUS_MESSAGE =
    "Found more than one BRAINARD-ROOM-SETUP-v1 … END-BRAINARD-ROOM-SETUP block in the pasted text; " +
    "paste only the LLM's final answer.";
  var SUCCESS_MESSAGE =
    "Parsed — review the filled-in fields, then fill in the agent names, group, and project yourself " +
    "before creating the room.";

  var textarea = document.getElementById("room-setup-paste-input");
  var button = document.getElementById("room-setup-paste-button");
  var messageEl = document.getElementById("room-setup-paste-message");
  var modesDataEl = document.getElementById("room-modes-data");
  var detailsEl = document.getElementById("new-room-details");
  if (!textarea || !button || !messageEl) return; // not on the rooms list page

  var validModes = [];
  try {
    var modes = modesDataEl ? JSON.parse(modesDataEl.textContent || "{}") : {};
    validModes = Object.keys(modes);
  } catch (e) {
    // Deliberately fails OPEN here, not closed: an empty validModes skips
    // the mode check below entirely (see the `validModes.length > 0` guard
    // in parseBlock) rather than rejecting every mode. #room-modes-data is
    // always server-rendered (app/routers/ui_rooms.py), so this catch
    // should never actually run in practice -- but if it somehow did,
    // failing closed would turn a client-side rendering bug into "the
    // entire paste feature silently rejects every mode," which is a worse
    // outcome than just skipping this one best-effort, convenience-only
    // check. `create_room` (app/rooms.py) is the real validation authority
    // regardless and will reject an invalid mode itself either way.
    validModes = [];
  }

  function findAllIndices(haystack, needle) {
    var indices = [];
    var i = haystack.indexOf(needle);
    while (i !== -1) {
      indices.push(i);
      i = haystack.indexOf(needle, i + needle.length);
    }
    return indices;
  }

  function setMessage(text, kind) {
    messageEl.textContent = text;
    messageEl.className = "meta notice " + kind;
  }

  function fail(text) {
    setMessage(text, "danger");
  }

  function dispatchChange(el) {
    var event;
    try {
      event = new Event("change", { bubbles: true });
    } catch (e) {
      event = document.createEvent("Event");
      event.initEvent("change", true, true);
    }
    el.dispatchEvent(event);
  }

  // Returns {ok: true, fields: {...}} or {ok: false, message: "..."} --
  // never touches the DOM itself; the caller applies fields only once this
  // whole function has succeeded (atomic: fully validated structurally
  // before anything is written, or nothing is written at all).
  function parseBlock(raw) {
    var starts = findAllIndices(raw, SENTINEL_START);
    var ends = findAllIndices(raw, SENTINEL_END);
    if (starts.length === 0 || ends.length === 0) {
      return { ok: false, message: NOT_FOUND_MESSAGE };
    }
    if (starts.length > 1 || ends.length > 1) {
      return { ok: false, message: AMBIGUOUS_MESSAGE };
    }
    var bodyStart = starts[0] + SENTINEL_START.length;
    var bodyEnd = ends[0];
    if (bodyEnd < bodyStart) {
      return { ok: false, message: NOT_FOUND_MESSAGE };
    }
    var body = raw.slice(bodyStart, bodyEnd);

    var lines = body.split(/\r\n|\r|\n/);
    var fields = {};
    var seenKeys = {};
    for (var i = 0; i < lines.length; i++) {
      var rawLine = lines[i];
      var trimmedLine = rawLine.trim();
      if (trimmedLine === "") continue; // blank lines between sentinels are tolerated

      var colonIdx = trimmedLine.indexOf(":");
      var key = colonIdx === -1 ? "" : trimmedLine.slice(0, colonIdx).trim();
      if (colonIdx === -1 || !KEY_PATTERN.test(key)) {
        return {
          ok: false,
          message: "The pasted block has a line that doesn't match the expected `key: value` format: \"" + trimmedLine + "\".",
        };
      }
      if (VALID_KEYS.indexOf(key) === -1) {
        return {
          ok: false,
          message:
            "The pasted block uses an unrecognized key '" + key + "'. Recognized keys: " + VALID_KEYS.join(", ") + ".",
        };
      }
      if (Object.prototype.hasOwnProperty.call(seenKeys, key)) {
        return { ok: false, message: "The key '" + key + "' appears more than once in the pasted block." };
      }
      seenKeys[key] = true;
      fields[key] = trimmedLine.slice(colonIdx + 1).trim();
    }

    if (!fields.name) {
      return { ok: false, message: "The pasted block is missing a required `name` value." };
    }
    if (!fields.mode) {
      return { ok: false, message: "The pasted block is missing a required `mode` value." };
    }
    // An empty validModes (the modes data was missing/unparseable, see the
    // catch above) skips this check by design -- create_room still
    // validates `mode` server-side regardless.
    if (validModes.length > 0 && validModes.indexOf(fields.mode) === -1) {
      return {
        ok: false,
        message:
          "The pasted block's `mode` value '" + fields.mode + "' is not one of the valid modes: " + validModes.join(", ") + ".",
      };
    }
    if (fields.mode !== "freeform" && !fields.topic) {
      return {
        ok: false,
        message: "The pasted block's mode '" + fields.mode + "' requires a non-blank `topic`, but none was given.",
      };
    }

    for (var k = 0; k < INTEGER_KEYS.length; k++) {
      var ikey = INTEGER_KEYS[k];
      var ival = fields[ikey];
      if (ival !== undefined && ival !== "" && !INTEGER_PATTERN.test(ival)) {
        return {
          ok: false,
          message: "The pasted block's `" + ikey + "` value must be a plain integer, got \"" + ival + "\".",
        };
      }
    }

    return { ok: true, fields: fields };
  }

  function applyFields(fields) {
    var touched = [];

    Object.keys(FIELD_TARGET_IDS).forEach(function (key) {
      var value = fields[key];
      if (value === undefined || value === "") return; // blank/omitted -- leave this field alone
      var el = document.getElementById(FIELD_TARGET_IDS[key]);
      if (!el) return;
      el.value = value;
      touched.push(el);
    });

    // duration_minutes has no single form field -- map it onto the
    // existing custom-duration controls (duration_preset + custom fields),
    // the same fields the owner would otherwise fill in by hand for a
    // duration the presets don't already cover.
    var durationMinutes = fields.duration_minutes;
    if (durationMinutes !== undefined && durationMinutes !== "") {
      var presetEl = document.getElementById("duration_preset");
      var valueEl = document.getElementById("custom_duration_value");
      var unitEl = document.getElementById("custom_duration_unit");
      if (unitEl) {
        unitEl.value = "minutes";
        touched.push(unitEl);
      }
      if (valueEl) {
        valueEl.value = durationMinutes;
        touched.push(valueEl);
      }
      if (presetEl) {
        presetEl.value = "custom";
        touched.push(presetEl);
      }
    }

    // Fire `change` after every field is set (not one at a time) so
    // room_form.js's listeners (updateAgentLabels/updateTopicRequired/
    // updateCustomDurationVisibility) re-run against the fully-applied
    // state rather than an intermediate one.
    touched.forEach(dispatchChange);

    if (detailsEl) detailsEl.open = true;
  }

  button.addEventListener("click", function () {
    var result = parseBlock(textarea.value || "");
    if (!result.ok) {
      fail(result.message);
      return;
    }
    applyFields(result.fields);
    setMessage(SUCCESS_MESSAGE, "ok");
  });
})();
