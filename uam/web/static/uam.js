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

// Broken app icons: an <img data-icon-fallback="X"> whose image can't be loaded is swapped
// for the same letter tile shown for apps without an icon. Error events don't bubble, so the
// listener runs in the capture phase; images that failed before this script ran are found by
// scanning once the page has loaded.
(function () {
  "use strict";

  function useFallback(img) {
    var tile = document.createElement("span");
    tile.className = img.className + " placeholder";
    tile.setAttribute("aria-hidden", "true");
    tile.textContent = img.getAttribute("data-icon-fallback") || "?";
    img.replaceWith(tile);
  }

  function isBroken(img) {
    return img.complete && img.naturalWidth === 0 && img.getAttribute("src");
  }

  document.addEventListener(
    "error",
    function (event) {
      var img = event.target;
      if (img.tagName === "IMG" && img.hasAttribute("data-icon-fallback")) {
        useFallback(img);
      }
    },
    true
  );

  function scan(root) {
    Array.prototype.forEach.call(root.querySelectorAll("img[data-icon-fallback]"), function (img) {
      if (isBroken(img)) {
        useFallback(img);
      }
    });
  }

  scan(document);
  document.addEventListener("htmx:afterSettle", function (event) {
    scan(event.detail.elt);
  });
})();
