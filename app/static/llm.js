// The Brain -- LLM provider model discovery (/ui/llm's "Fetch models"
// button). This page is already owner-only end to end (require_ui_session
// on every route), so no extra client-side gating is needed here.
//
// XSS: the model ids POST /ui/llm/models returns are PROVIDER-supplied,
// not app-trusted content -- a hostile or misconfigured provider could
// return an id like "<img src=x onerror=...>" or a quote-breakout string.
// This file renders them via document.createElement + .textContent ONLY,
// exactly the same discipline app/static/room_ai.js's model-output
// rendering uses -- NEVER innerHTML, and NEVER a template literal or
// string concatenation fed into innerHTML.
(function () {
  "use strict";

  var config = document.getElementById("llm-config");
  if (!config) return; // not on the /ui/llm page

  var fetchBtn = document.getElementById("llm-fetch-models-btn");
  var errorEl = document.getElementById("llm-models-error");
  var pickerWrap = document.getElementById("llm-models-picker-wrap");
  var picker = document.getElementById("llm-models-picker");
  var truncatedNote = document.getElementById("llm-models-truncated-note");
  var baseUrlInput = document.getElementById("base_url");
  var apiKeyInput = document.getElementById("api_key");
  var modelInput = document.getElementById("model");
  if (!fetchBtn || !errorEl || !pickerWrap || !picker || !modelInput || !baseUrlInput) return;

  var csrfToken = config.dataset.csrf || "";

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

  function hidePicker() {
    clearChildren(picker);
    var placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "-- select a discovered model --";
    picker.appendChild(placeholder);
    pickerWrap.style.display = "none";
    truncatedNote.style.display = "none";
    truncatedNote.textContent = "";
  }

  function renderModels(models, truncated) {
    clearChildren(picker);
    var placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = models.length ? "-- select a discovered model (" + models.length + ") --" : "-- no models returned --";
    picker.appendChild(placeholder);
    models.forEach(function (id) {
      var opt = document.createElement("option");
      opt.value = id; // .value is always a literal string, never parsed as markup
      opt.textContent = id; // provider-supplied, untrusted -- textContent only, see file header
      picker.appendChild(opt);
    });
    pickerWrap.style.display = "";
    if (truncated) {
      truncatedNote.textContent = "The provider returned more than " + models.length + " models; showing the first " + models.length + " (sorted).";
      truncatedNote.style.display = "";
    } else {
      truncatedNote.style.display = "none";
      truncatedNote.textContent = "";
    }
    if (!models.length) {
      showError("The provider responded but listed no models.");
    }
  }

  fetchBtn.addEventListener("click", function () {
    clearError();
    hidePicker();
    var originalLabel = fetchBtn.textContent;
    fetchBtn.disabled = true;
    fetchBtn.textContent = "Fetching…";

    var body =
      "base_url=" + encodeURIComponent(baseUrlInput.value || "") +
      "&api_key=" + encodeURIComponent(apiKeyInput ? apiKeyInput.value || "" : "") +
      "&csrf_token=" + encodeURIComponent(csrfToken);

    fetch("/ui/llm/models", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body,
    })
      .then(function (resp) {
        return resp.json().then(function (data) {
          return { ok: resp.ok, data: data };
        });
      })
      .then(function (outcome) {
        if (!outcome.ok) {
          var err = outcome.data && outcome.data.error;
          showError((err && err.detail) || "Fetching models failed.");
          return;
        }
        renderModels(outcome.data.models || [], !!outcome.data.truncated);
      })
      .catch(function () {
        showError("Network error fetching models. Try again.");
      })
      .then(function () {
        fetchBtn.disabled = false;
        fetchBtn.textContent = originalLabel;
      });
  });

  picker.addEventListener("change", function () {
    if (picker.value) {
      modelInput.value = picker.value; // plain .value assignment -- never parsed as markup
    }
  });
})();
