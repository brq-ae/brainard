// The Brain -- Room AI actions (ADR-0011). This page (/ui/rooms/{id}) is
// already owner-only end to end (require_ui_session on every route), so no
// extra client-side gating is needed here.
//
// XSS: every field in an action's JSON result is MODEL OUTPUT derived from
// the room's transcript, which agents (not the owner) write -- a crafted
// message could try to get the model to emit attacker-chosen text
// (ADR-0011 decision 4). This file renders that content via
// document.createElement + .textContent ONLY -- exactly the same
// discipline app/static/rooms.js's message rendering uses -- NEVER
// innerHTML, and NEVER a template literal or string concatenation fed into
// innerHTML. The deposit form's fields are populated via plain .value
// assignment, which the browser always treats as a literal string, never
// parsed as markup, whether the target is a text <input> or a <textarea>.
(function () {
  "use strict";

  var config = document.getElementById("room-config");
  if (!config) return; // not on a room view page

  var actionsContainer = document.getElementById("room-ai-actions");
  var resultEl = document.getElementById("room-ai-result");
  var errorEl = document.getElementById("room-ai-error");
  var depositForm = document.getElementById("room-ai-deposit-form");
  if (!actionsContainer || !resultEl || !errorEl || !depositForm) return;

  var roomId = config.dataset.roomId;
  var csrfToken = config.dataset.csrf || "";
  var depositTitle = document.getElementById("room-ai-deposit-title");
  var depositBody = document.getElementById("room-ai-deposit-body");
  var depositNamespace = document.getElementById("room-ai-deposit-namespace");

  function clearChildren(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  function showError(message) {
    clearChildren(errorEl);
    errorEl.appendChild(document.createTextNode(message));
    errorEl.style.display = "";
  }

  function clearError() {
    clearChildren(errorEl);
    errorEl.style.display = "none";
  }

  function labeledParagraph(label, text) {
    var p = document.createElement("p");
    var strong = document.createElement("strong");
    strong.textContent = label + ": ";
    p.appendChild(strong);
    p.appendChild(document.createTextNode(text));
    return p;
  }

  function bulletList(items) {
    var ul = document.createElement("ul");
    items.forEach(function (item) {
      var li = document.createElement("li");
      li.textContent = item;
      ul.appendChild(li);
    });
    return ul;
  }

  function useResultButton(label, title, body, namespace) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn";
    btn.textContent = label;
    btn.addEventListener("click", function () {
      depositTitle.value = title;
      depositBody.value = body;
      depositNamespace.value = namespace;
      depositForm.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    return btn;
  }

  function renderSummarize(roomName, result) {
    var frag = document.createDocumentFragment();
    frag.appendChild(labeledParagraph("Summary", result.summary || ""));
    var keyPoints = result.key_points || [];
    if (keyPoints.length) {
      frag.appendChild(bulletList(keyPoints));
    }
    var bodyLines = [result.summary || ""];
    if (keyPoints.length) {
      bodyLines.push("", "Key points:");
      keyPoints.forEach(function (p) {
        bodyLines.push("- " + p);
      });
    }
    frag.appendChild(useResultButton("Use this result", "Room summary: " + roomName, bodyLines.join("\n"), "reference"));
    return frag;
  }

  function renderVerdict(roomName, result) {
    var frag = document.createDocumentFragment();
    var winner = result.winner || "(no clear winner)";
    frag.appendChild(labeledParagraph("Winner", winner));
    frag.appendChild(labeledParagraph("Reasoning", result.reasoning || ""));
    if (result.strongest_for) frag.appendChild(labeledParagraph("Strongest for", result.strongest_for));
    if (result.strongest_against) frag.appendChild(labeledParagraph("Strongest against", result.strongest_against));

    var bodyLines = ["Winner: " + winner, "", "Reasoning: " + (result.reasoning || "")];
    if (result.strongest_for) bodyLines.push("", "Strongest for: " + result.strongest_for);
    if (result.strongest_against) bodyLines.push("", "Strongest against: " + result.strongest_against);
    frag.appendChild(useResultButton("Use this result", "Verdict: " + roomName, bodyLines.join("\n"), "reference"));
    return frag;
  }

  function renderDecisions(roomName, result) {
    var frag = document.createDocumentFragment();
    var decisions = result.decisions || [];
    var actionItems = result.action_items || [];

    frag.appendChild(labeledParagraph("Decisions", decisions.length ? "" : "(none)"));
    if (decisions.length) frag.appendChild(bulletList(decisions));
    frag.appendChild(labeledParagraph("Action items", actionItems.length ? "" : "(none)"));
    if (actionItems.length) frag.appendChild(bulletList(actionItems));

    var bodyParts = [];
    if (decisions.length) {
      bodyParts.push("Decisions:\n" + decisions.map(function (d) { return "- " + d; }).join("\n"));
    }
    if (actionItems.length) {
      bodyParts.push("Action items:\n" + actionItems.map(function (a) { return "- " + a; }).join("\n"));
    }
    frag.appendChild(
      useResultButton("Use this result", "Decisions & action items: " + roomName, bodyParts.join("\n\n"), "howto")
    );
    return frag;
  }

  function renderLessons(_roomName, result) {
    var frag = document.createDocumentFragment();
    var lessons = result.lessons || [];
    if (!lessons.length) {
      var p = document.createElement("p");
      p.textContent = "No lessons extracted.";
      frag.appendChild(p);
      return frag;
    }
    lessons.forEach(function (lesson) {
      var card = document.createElement("div");
      card.className = "row";
      var title = document.createElement("strong");
      title.textContent = lesson.title;
      var body = document.createElement("p");
      body.textContent = lesson.body;
      card.appendChild(title);
      card.appendChild(body);
      card.appendChild(useResultButton("Use this lesson", lesson.title, lesson.body, "lessons"));
      frag.appendChild(card);
    });
    return frag;
  }

  var RENDERERS = {
    summarize: renderSummarize,
    verdict: renderVerdict,
    decisions: renderDecisions,
    lessons: renderLessons,
  };

  function runAction(action, roomName) {
    clearError();
    clearChildren(resultEl);
    var note = document.createElement("p");
    note.className = "meta";
    note.textContent = "Running " + action + "…";
    resultEl.appendChild(note);

    fetch("/ui/rooms/" + encodeURIComponent(roomId) + "/ai/" + encodeURIComponent(action), {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "csrf_token=" + encodeURIComponent(csrfToken),
    })
      .then(function (resp) {
        return resp.json().then(function (data) {
          return { ok: resp.ok, data: data };
        });
      })
      .then(function (outcome) {
        clearChildren(resultEl);
        if (!outcome.ok) {
          var err = outcome.data && outcome.data.error;
          showError((err && err.detail) || "The action failed.");
          if (err && err.code === "no_llm_provider_configured") {
            errorEl.appendChild(document.createTextNode(" "));
            var link = document.createElement("a");
            link.href = "/ui/llm";
            link.textContent = "Configure an LLM provider";
            errorEl.appendChild(link);
          }
          return;
        }
        if (outcome.data.truncated_notice) {
          var notice = document.createElement("p");
          notice.className = "meta";
          notice.textContent = outcome.data.truncated_notice;
          resultEl.appendChild(notice);
        }
        var renderer = RENDERERS[outcome.data.action];
        if (renderer) {
          resultEl.appendChild(renderer(roomName, outcome.data.result || {}));
        }
      })
      .catch(function () {
        clearChildren(resultEl);
        showError("Network error running the action. Try again.");
      });
  }

  actionsContainer.addEventListener("click", function (event) {
    var btn = event.target.closest && event.target.closest("[data-room-ai-action]");
    if (!btn) return;
    runAction(btn.getAttribute("data-room-ai-action"), actionsContainer.dataset.roomName || "");
  });
})();
