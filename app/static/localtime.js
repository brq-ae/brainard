// The Brain -- owner-facing local-timezone display (display-only; no
// stored value, API response, join prompt, bootstrap, export, deposit, or
// notification is ever touched by this file -- those all stay UTC, see
// app/templates_env.py's `human_ts` filter docstring).
//
// The container runs with no TZ set (everything server-side is UTC), and
// the owner reads this UI from a fixed offset of his own. Every
// server-rendered timestamp already carries its exact UTC instant in a
// `<time datetime="...">` element (see `human_ts` above); this file's only
// job is to rewrite each such element's visible text, in the browser, to
// the viewer's own local time -- using nothing but the built-in Intl/Date
// APIs (no date library, per this repo's dependency policy).
//
// Same treatment for messages app/static/rooms.js appends live via
// polling: it builds its own <time datetime="..."> element per message and
// calls BrainTime.convertOne on it below, so a transcript never mixes
// already-converted older rows with a raw-UTC just-arrived one.
(function () {
  "use strict";

  function pad(n) {
    return n < 10 ? "0" + n : String(n);
  }

  // Formats a Date (read via its LOCAL getters, i.e. already shifted to
  // the browser's timezone) as "YYYY-MM-DD HH:MM UTC+H[:MM]" -- the same
  // shape as the server's own UTC rendering ("YYYY-MM-DD HH:MM UTC", see
  // _human_ts_filter) but with the fixed "UTC" suffix swapped for the
  // viewer's actual signed offset. Deliberately not a bare "HH:MM" or a
  // locale-formatted string: the explicit offset is what makes a
  // converted value impossible to mistake for a stray un-converted UTC one
  // elsewhere on the page.
  function formatLocal(date) {
    var offsetMin = -date.getTimezoneOffset(); // minutes EAST of UTC
    var sign = offsetMin < 0 ? "-" : "+";
    var absMin = Math.abs(offsetMin);
    var offsetHours = Math.floor(absMin / 60);
    var offsetMinutes = absMin % 60;
    var offset = "UTC" + sign + offsetHours + (offsetMinutes ? ":" + pad(offsetMinutes) : "");
    return (
      date.getFullYear() +
      "-" +
      pad(date.getMonth() + 1) +
      "-" +
      pad(date.getDate()) +
      " " +
      pad(date.getHours()) +
      ":" +
      pad(date.getMinutes()) +
      " " +
      offset
    );
  }

  // Converts one <time datetime="..."> element's visible text in place.
  // `.textContent` only, per this codebase's XSS discipline (see rooms.js/
  // room_attachments.js file headers) -- never innerHTML. If the
  // `datetime` attribute is missing or unparseable, the element is left
  // exactly as rendered (the existing UTC text) rather than blanked.
  function convertOne(timeEl) {
    var iso = timeEl.getAttribute("datetime");
    if (!iso) return;
    var date = new Date(iso);
    if (isNaN(date.getTime())) return;
    var local = formatLocal(date);
    timeEl.textContent = local;
    timeEl.title = "Your local time (converted from UTC in your browser)";
  }

  function convertAll(root) {
    var elements = (root || document).querySelectorAll("time[data-local-ts]");
    for (var i = 0; i < elements.length; i++) {
      convertOne(elements[i]);
    }
  }

  convertAll(document);

  // Exposed so app/static/rooms.js can convert the <time> elements it
  // builds itself for messages appended live by the short-poll loop,
  // without duplicating formatLocal's logic.
  window.BrainTime = { convertOne: convertOne, convertAll: convertAll, formatLocal: formatLocal };
})();
