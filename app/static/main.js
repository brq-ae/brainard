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

  // Copy-to-clipboard button, shared by every [data-copy-target] button on
  // the site (the machine-token reveal, "Copy transcript (Markdown)", the
  // export copy button, and, as of ADR-0013, the per-participant
  // "Copy join prompt" buttons and the "Copy prompt for {{ m }}" buttons in
  // the bottom Join prompts section).
  //
  // ADR-0013: three-tier fallback, in order:
  //   1. navigator.clipboard.writeText -- the modern API, but only
  //      available in a secure context (HTTPS or localhost). The owner
  //      reaches this UI over plain HTTP by LAN IP, which is neither, so
  //      this tier is never available there -- not a rare edge case, but
  //      that deployment's permanent condition on every page load.
  //   2. A hidden <textarea> + document.execCommand('copy'). Deprecated,
  //      but still functions in non-secure contexts in every current
  //      browser, unlike tier 1 -- this is what actually gives the LAN
  //      owner real one-click copying.
  //   3. window.prompt(...) -- a manual-copy dialog, the last resort for
  //      the rare browser where even execCommand is unavailable.
  //
  // The "Copied!"/"Copy failed" confirmation is driven by the ACTUAL
  // result of tiers 1/2, never fired optimistically -- a silent clipboard
  // failure that still says "Copied!" would send the owner on to paste
  // stale clipboard contents into an agent's tab, which is the exact
  // failure mode this whole mechanism exists to prevent.
  document.addEventListener("click", function (event) {
    var btn = event.target.closest && event.target.closest("[data-copy-target]");
    if (!btn) return;
    var target = document.getElementById(btn.getAttribute("data-copy-target"));
    if (!target) return;
    var text = target.textContent || "";
    var original = btn.textContent;
    var resetTimer = null;

    var flip = function (label) {
      if (resetTimer) clearTimeout(resetTimer);
      btn.textContent = label;
      resetTimer = setTimeout(function () {
        btn.textContent = original;
        resetTimer = null;
      }, 1500);
    };
    var showSuccess = function () {
      flip("Copied!");
    };
    var showFailure = function () {
      flip("Copy failed");
    };

    // Tier 2: hidden textarea + document.execCommand('copy'). Written
    // carefully to avoid any visible side effect: positioned off-screen
    // (not display:none/visibility:hidden -- some browsers refuse to
    // focus/select content hidden that way) via fixed positioning so it
    // never affects page layout, focused with preventScroll so it never
    // causes a scroll jump, and the previous focus/selection are restored
    // afterwards. The element is always removed, in a finally, whether the
    // copy succeeded, failed, or threw.
    var execCommandCopy = function () {
      var textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.top = "0";
      textarea.style.left = "-9999px";
      textarea.style.width = "1px";
      textarea.style.height = "1px";
      document.body.appendChild(textarea);

      var previousFocus = document.activeElement;
      var selection = window.getSelection ? window.getSelection() : null;
      var previousRange = selection && selection.rangeCount > 0 ? selection.getRangeAt(0) : null;

      var ok = false;
      try {
        textarea.focus({ preventScroll: true });
        textarea.select();
        textarea.setSelectionRange(0, text.length);
        ok = document.execCommand("copy");
      } catch (err) {
        ok = false;
      } finally {
        document.body.removeChild(textarea);
        if (previousFocus && typeof previousFocus.focus === "function") {
          previousFocus.focus({ preventScroll: true });
        }
        if (selection) {
          selection.removeAllRanges();
          if (previousRange) {
            selection.addRange(previousRange);
          }
        }
      }
      return ok;
    };

    // Tier 2 then, on failure, tier 3 -- also used directly when tier 1
    // isn't available at all (the common case on this deployment).
    var fallToExecCommandThenPrompt = function () {
      if (execCommandCopy()) {
        showSuccess();
      } else {
        showFailure();
        window.prompt("Copy the token manually:", text);
      }
    };

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(showSuccess, fallToExecCommandThenPrompt);
    } else {
      fallToExecCommandThenPrompt();
    }
  });
})();
