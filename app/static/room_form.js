// The Brain -- Rooms create-form dynamic behavior (ADR-0007: room modes and
// time limits, Part 2 UI). Vanilla JS, no framework, no CDN. No inline
// event-handler attributes (CSP-friendly, matches app/static/main.js): every
// listener is attached here via addEventListener, keyed off element ids
// rendered by app/templates/rooms_list.html.
//
// Mode data comes from a <script type="application/json" id="room-modes-data">
// block, sourced from app/room_modes.py's ROOM_MODES via
// app/routers/ui_rooms.py's ROOM_MODES_JSON -- never a hardcoded, divergent
// mode list here. This file only reads that data to (a) toggle the two
// agent-name field labels to reflect the selected mode's sides, (b) toggle
// whether the topic field is presented as required, and (c) show/hide the
// custom time-limit fields. All the actual validation (mode, topic
// requiredness, sides, deadline range) happens server-side in
// app/rooms.py's create_room -- this is presentation only, and a
// JS-disabled submission still works correctly (the server enforces
// everything and re-renders a clean error if something's missing/invalid).
(function () {
  "use strict";

  var dataEl = document.getElementById("room-modes-data");
  var modeSelect = document.getElementById("mode");
  if (!dataEl || !modeSelect) return; // not on the create-room form

  var modes = {};
  try {
    modes = JSON.parse(dataEl.textContent || "{}");
  } catch (e) {
    return; // malformed data -- fail closed, leave the static labels alone
  }

  var agentALabel = document.getElementById("agent_a_label");
  var agentBLabel = document.getElementById("agent_b_label");
  var topicInput = document.getElementById("topic");
  var topicLabel = document.getElementById("topic-label");
  var durationPreset = document.getElementById("duration_preset");
  var customFields = document.getElementById("custom-duration-fields");

  // Mode-specific phrasing layered on top of the data-driven side_labels
  // (e.g. ["For", "Against"] for debate, ["Proposer", "Critic"] for
  // critique). The "Agent arguing X" framing is presentational wording, not
  // a domain rule -- app/room_modes.py's side_labels stay the plain
  // "For"/"Against" strings the join prompt itself uses. Any asymmetric
  // mode not listed here just uses its side_labels value as-is.
  var SIDE_PHRASING = {
    debate: function (label) {
      return "Agent arguing " + label.toUpperCase();
    },
  };

  function updateAgentLabels() {
    if (!agentALabel || !agentBLabel) return;
    var mode = modes[modeSelect.value];
    if (!mode || mode.symmetric || !mode.side_labels) {
      agentALabel.textContent = "Agent 1 (name)";
      agentBLabel.textContent = "Agent 2 (name)";
      return;
    }
    var phrase =
      SIDE_PHRASING[modeSelect.value] ||
      function (label) {
        return label;
      };
    agentALabel.textContent = phrase(mode.side_labels[0]) + " (name)";
    agentBLabel.textContent = phrase(mode.side_labels[1]) + " (name)";
  }

  function updateTopicRequired() {
    if (!topicInput) return;
    var isFreeform = modeSelect.value === "freeform";
    topicInput.required = !isFreeform;
    if (topicLabel) {
      topicLabel.textContent = isFreeform ? "Topic" : "Topic (required)";
    }
  }

  function updateCustomDurationVisibility() {
    if (!durationPreset || !customFields) return;
    customFields.style.display = durationPreset.value === "custom" ? "" : "none";
  }

  modeSelect.addEventListener("change", function () {
    updateAgentLabels();
    updateTopicRequired();
  });
  if (durationPreset) {
    durationPreset.addEventListener("change", updateCustomDurationVisibility);
  }

  // Initialize on load -- the form may be re-rendered with a previously
  // selected mode/preset after a validation error, so state must reflect
  // the server-rendered <select> value, not just future change events.
  updateAgentLabels();
  updateTopicRequired();
  updateCustomDurationVisibility();
})();
