// Busy feedback for every HTMX action: while a button's (or a form's) request is running,
// its buttons are disabled and show Pico's spinner (aria-busy). Polling elements are untouched.
(function () {
  "use strict";

  function buttonsFor(elt) {
    if (elt.tagName === "FORM") {
      return Array.prototype.slice.call(
        elt.querySelectorAll("button[type=submit], button:not([type]), input[type=submit]")
      );
    }
    if (elt.tagName === "BUTTON" || elt.getAttribute("role") === "button") {
      return [elt];
    }
    return [];
  }

  document.addEventListener("htmx:beforeRequest", function (event) {
    var buttons = buttonsFor(event.detail.elt).filter(function (b) {
      return !b.disabled;
    });
    buttons.forEach(function (b) {
      b.setAttribute("aria-busy", "true");
      b.disabled = true;
    });
    event.detail.elt.uamBusyButtons = buttons;
  });

  document.addEventListener("htmx:afterRequest", function (event) {
    (event.detail.elt.uamBusyButtons || []).forEach(function (b) {
      b.removeAttribute("aria-busy");
      b.disabled = false;
    });
    event.detail.elt.uamBusyButtons = [];
  });
})();
