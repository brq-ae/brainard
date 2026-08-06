// The Brain -- UI. Minimal vanilla JS, no framework, no CDN.
// No inline event-handler attributes anywhere (CSP-friendly): every
// listener below is attached here, via addEventListener, keyed off
// data-* attributes in the markup.
(function () {
  "use strict";

  // Confirm before submitting any form marked data-confirm="<message>"
  // (used by the machine-revoke form).
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form instanceof HTMLFormElement && form.dataset.confirm) {
      if (!window.confirm(form.dataset.confirm)) {
        event.preventDefault();
      }
    }
  });

  // Copy-to-clipboard button for the one-time-shown machine token.
  document.addEventListener("click", function (event) {
    var btn = event.target.closest && event.target.closest("[data-copy-target]");
    if (!btn) return;
    var target = document.getElementById(btn.getAttribute("data-copy-target"));
    if (!target) return;
    var text = target.textContent || "";
    var done = function () {
      var original = btn.textContent;
      btn.textContent = "Copied!";
      setTimeout(function () {
        btn.textContent = original;
      }, 1500);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () {
        window.prompt("Copy the token manually:", text);
      });
    } else {
      window.prompt("Copy the token manually:", text);
    }
  });
})();
