// The Brain -- "Copy doctrine" checklist rebuild (ADR-0019 decision 4).
// Vanilla JS, no framework, no CDN. No inline event-handler attributes
// (CSP-friendly, matches app/static/main.js).
//
// Listens for `change` on the doctrine checklist (app/templates/doctrine.html)
// and rebuilds the hidden `#doctrine-briefing-text` element's `.textContent`
// from exactly the currently-checked boxes' `data-rule-text` values, each
// copied whole and unaltered -- rules are included or excluded, never
// paraphrased or trimmed. The initial page load already renders that hidden
// element correctly from the server-computed default-checked set
// (app/doctrine.py's `doctrine_copy_text`, called once by
// app/routers/ui_doctrine.py), so a copy before ever touching a checkbox
// works with zero JS execution -- this file only keeps it in sync with
// whatever the owner ticks/unticks afterward. This is additive to, not a
// replacement of, the shared [data-copy-target] click handler
// (app/static/main.js, ADR-0013 decision 5, unmodified) -- that handler
// still does the actual copying, reading whatever `.textContent` this file
// last wrote.
(function () {
  "use strict";

  var checklist = document.getElementById("doctrine-checklist");
  var target = document.getElementById("doctrine-briefing-text");
  if (!checklist || !target) return; // no global doctrine posted yet -- page omits this section entirely

  var versionLabel = checklist.getAttribute("data-doctrine-version") || "";

  // Mirrors app/doctrine.py's `doctrine_copy_text` framing text exactly
  // (same English sentence, only `versionLabel` varies) -- kept as a
  // literal here, not fetched, since there is no server round trip once
  // the page has loaded. Duplicated hand-written prose across a
  // server-render path and a client-rebuild path is an accepted, existing
  // pattern in this codebase (ADR-0019's own "anti-drift" note: the
  // numbers/enums are single-sourced, the wrapping English sentences are
  // still hand-maintained wherever they're rendered).
  function framingText() {
    return (
      "These are operating rules from my Brainard doctrine (" +
      versionLabel +
      ") that I want you to adopt for the remainder of this conversation. They are a deliberately chosen " +
      "subset of a larger doctrine: the rules below are the ones that make sense without access to my " +
      "system; the absence of a rule here doesn't mean it doesn't apply elsewhere, only that it doesn't " +
      "translate to a conversation like this one."
    );
  }

  function rebuild() {
    var checkboxes = checklist.querySelectorAll('input[type="checkbox"]');
    var lines = [];
    for (var i = 0; i < checkboxes.length; i++) {
      var box = checkboxes[i];
      if (!box.checked) continue;
      var id = box.getAttribute("data-rule-id") || "";
      var text = box.getAttribute("data-rule-text") || "";
      lines.push("- " + id + ": " + text);
    }
    target.textContent = lines.length > 0 ? framingText() + "\n\n" + lines.join("\n") : framingText();
  }

  checklist.addEventListener("change", function (event) {
    if (event.target && event.target.matches && event.target.matches('input[type="checkbox"]')) {
      rebuild();
    }
  });
})();
