// The Brain -- Room file attachments (ADR-0012). Two JS-driven pieces on
// this page (/ui/rooms/{id}): the upload button (the request body IS the
// file's raw bytes -- a `fetch(url, {body: file})`, never FastAPI
// multipart/UploadFile parsing, so nothing buffers the whole file before
// app/attachments.py's streaming validation even starts) and the
// "Attach from Brain" live search picker. Every other attachments action
// (delete, Save to Brain, attach-from-Brain's actual POST, the
// agent-uploads checkbox) is an ordinary HTML form submit -- no JS needed,
// same CSRF hidden-field discipline as the rest of this app.
//
// XSS: search results are SERVER content (Brain document titles -- owner/
// agent-authored, untrusted) rendered into the DOM here. Exactly the same
// discipline app/static/rooms.js's message rendering and app/static/
// room_ai.js's result rendering use: document.createElement +
// .textContent ONLY, never innerHTML, never a template literal fed into
// innerHTML.
(function () {
  "use strict";

  var config = document.getElementById("room-config");
  if (!config) return; // not on a room view page

  var roomId = config.dataset.roomId;
  var csrfToken = config.dataset.csrf || "";

  // --- Upload ---

  var fileInput = document.getElementById("attachment-file-input");
  var uploadBtn = document.getElementById("attachment-upload-btn");
  var uploadError = document.getElementById("attachment-upload-error");

  function showUploadError(message) {
    if (!uploadError) return;
    uploadError.textContent = message;
    uploadError.style.display = "";
  }

  if (uploadBtn && fileInput) {
    uploadBtn.addEventListener("click", function () {
      var file = fileInput.files && fileInput.files[0];
      if (!file) {
        showUploadError("Choose a PDF file first.");
        return;
      }
      if (uploadError) uploadError.style.display = "none";
      uploadBtn.disabled = true;
      var originalLabel = uploadBtn.textContent;
      uploadBtn.textContent = "Uploading…";

      fetch(
        "/ui/rooms/" + encodeURIComponent(roomId) + "/attachments?filename=" + encodeURIComponent(file.name),
        {
          method: "POST",
          credentials: "same-origin",
          headers: { "X-CSRF-Token": csrfToken },
          body: file,
        }
      )
        .then(function (resp) {
          return resp.json().then(function (data) {
            return { ok: resp.ok, data: data };
          });
        })
        .then(function (outcome) {
          if (!outcome.ok) {
            var err = outcome.data && outcome.data.error;
            showUploadError((err && err.detail) || "Upload failed.");
            uploadBtn.disabled = false;
            uploadBtn.textContent = originalLabel;
            return;
          }
          window.location.reload();
        })
        .catch(function () {
          showUploadError("Network error uploading the file. Try again.");
          uploadBtn.disabled = false;
          uploadBtn.textContent = originalLabel;
        });
    });
  }

  // --- Attach from Brain: live search picker ---

  var searchInput = document.getElementById("attach-search-input");
  var resultsList = document.getElementById("attach-search-results");
  var emptyNotice = document.getElementById("attach-search-empty");
  var searchDebounce = null;

  function clearChildren(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  // One search result -> a small form (document_id + csrf, ordinary POST,
  // same as every other non-JS-driven action on this page) rather than a
  // JS-fetched attach -- keeps the actual attach request an unremarkable,
  // CSRF-form-field-protected navigation.
  function renderResult(result) {
    var li = document.createElement("li");
    li.className = "row";

    var main = document.createElement("span");
    main.className = "row-main";
    var strong = document.createElement("strong");
    strong.textContent = result.title; // textContent -- inert even if hostile
    main.appendChild(strong);
    main.appendChild(document.createTextNode(" "));
    var meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = "(" + result.type + (result.project ? ", " + result.project : "") + ")";
    main.appendChild(meta);

    var form = document.createElement("form");
    form.method = "post";
    form.action = "/ui/rooms/" + encodeURIComponent(roomId) + "/attach-from-brain";
    form.className = "inline-form";

    var csrfField = document.createElement("input");
    csrfField.type = "hidden";
    csrfField.name = "csrf_token";
    csrfField.value = csrfToken;
    form.appendChild(csrfField);

    var docIdField = document.createElement("input");
    docIdField.type = "hidden";
    docIdField.name = "document_id";
    docIdField.value = result.id;
    form.appendChild(docIdField);

    var submitBtn = document.createElement("button");
    submitBtn.type = "submit";
    submitBtn.className = "btn";
    submitBtn.textContent = "Attach";
    form.appendChild(submitBtn);

    li.appendChild(main);
    li.appendChild(form);
    return li;
  }

  function runSearch(q) {
    fetch("/ui/rooms/" + encodeURIComponent(roomId) + "/attach-search?q=" + encodeURIComponent(q), {
      credentials: "same-origin",
    })
      .then(function (resp) {
        return resp.json();
      })
      .then(function (data) {
        if (!resultsList) return;
        clearChildren(resultsList);
        var results = data.results || [];
        if (emptyNotice) emptyNotice.style.display = results.length ? "none" : "";
        results.forEach(function (result) {
          resultsList.appendChild(renderResult(result));
        });
      })
      .catch(function () {
        // Network hiccup -- leave whatever results are already shown; the
        // owner can just retype to retry.
      });
  }

  if (searchInput && resultsList) {
    searchInput.addEventListener("input", function () {
      var q = searchInput.value;
      if (searchDebounce) clearTimeout(searchDebounce);
      if (!q.trim()) {
        clearChildren(resultsList);
        if (emptyNotice) emptyNotice.style.display = "none";
        return;
      }
      searchDebounce = setTimeout(function () {
        runSearch(q);
      }, 300);
    });
  }
})();
