// The Brain -- Agent Chat Rooms live view (ADR-0006 phase B).
//
// SHORT-POLL ONLY: this file polls GET /ui/rooms/{id}/messages?since=<seq>
// on a plain ~2s setTimeout loop and stops once the room is closed. It
// never long-polls -- the server endpoint it calls always returns
// immediately (wait=0; see app/routers/ui_rooms.py's module docstring for
// why the owner UI must never hold a long-poll connection open).
//
// XSS: `sender` and `text` in the poll response are untrusted, hostile-
// capable content -- an agent (or the owner) could post a `<script>` tag or
// an `<img src=x onerror=...>`. Every message this file appends to the DOM
// is built with document.createElement + .textContent /
// document.createTextNode. NEVER innerHTML, and NEVER a template literal or
// string concatenation fed into innerHTML -- that is exactly the mistake
// that would let a posted `<script>`/`<img onerror>` execute. textContent
// and createTextNode always insert their argument as literal text, never
// parsed as markup, so hostile message content renders inert no matter what
// it contains.
(function () {
  "use strict";

  var POLL_INTERVAL_MS = 2000;

  var config = document.getElementById("room-config");
  if (!config) return; // not on a room view page

  var roomId = config.dataset.roomId;
  var lastSeq = parseInt(config.dataset.lastSeq, 10) || 0;
  var initialStatus = config.dataset.status;

  var list = document.getElementById("room-messages");
  var emptyNotice = document.getElementById("room-messages-empty");
  var countEl = document.getElementById("room-count");
  var statusBadge = document.getElementById("room-status-badge");
  var closedBanner = document.getElementById("room-closed-banner");
  var closeReasonEl = document.getElementById("room-close-reason");
  var postPanel = document.getElementById("room-post-panel");
  var stopPanel = document.getElementById("room-stop-panel");

  // Appends one message row. Built entirely with createElement/textContent
  // -- see file header. `m.sender`/`m.text` are never passed to innerHTML.
  function appendMessage(m) {
    if (!list) return;
    var li = document.createElement("li");
    li.className = "row";
    li.dataset.seq = String(m.seq);

    var main = document.createElement("span");
    main.className = "row-main";

    var senderEl = document.createElement("strong");
    senderEl.textContent = m.sender; // textContent -- inert even if hostile
    main.appendChild(senderEl);
    main.appendChild(document.createTextNode(": "));
    main.appendChild(document.createTextNode(m.text)); // text node -- inert even if hostile

    var meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = m.created_at;

    li.appendChild(main);
    li.appendChild(meta);
    list.appendChild(li);

    if (emptyNotice) {
      emptyNotice.style.display = "none";
    }
  }

  function showClosed(closeReason) {
    if (statusBadge) {
      statusBadge.textContent = "closed";
      statusBadge.className = "badge inactive";
    }
    if (closedBanner) {
      closedBanner.style.display = "";
    }
    if (closeReasonEl && closeReason) {
      closeReasonEl.textContent = closeReason;
    }
    if (postPanel) postPanel.style.display = "none";
    if (stopPanel) stopPanel.style.display = "none";
  }

  function poll() {
    fetch("/ui/rooms/" + encodeURIComponent(roomId) + "/messages?since=" + lastSeq, {
      credentials: "same-origin",
    })
      .then(function (resp) {
        if (!resp.ok) throw new Error("poll failed: " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        var messages = data.messages || [];
        for (var i = 0; i < messages.length; i++) {
          appendMessage(messages[i]);
          if (messages[i].seq > lastSeq) lastSeq = messages[i].seq;
        }
        if (countEl && typeof data.message_count === "number") {
          countEl.textContent = String(data.message_count);
        }
        if (data.status === "closed") {
          showClosed(data.close_reason);
          return; // guardrail: stop polling once closed
        }
        setTimeout(poll, POLL_INTERVAL_MS);
      })
      .catch(function () {
        // Network hiccup, or the tab's session expired -- back off and
        // retry on the same interval rather than hammering or throwing.
        setTimeout(poll, POLL_INTERVAL_MS);
      });
  }

  if (initialStatus === "open") {
    setTimeout(poll, POLL_INTERVAL_MS);
  }
})();
