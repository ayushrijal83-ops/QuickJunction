/* Quick Junction UI behaviour (final UI pass). Progressive enhancement only:
 * every form, link and flash message works without this file. Nothing here
 * changes what is submitted -- the server stays the authority on every rule.
 *
 *  - theme toggle   [data-theme-toggle]   light/dark, remembered per browser
 *  - toasts         .qj-toast              auto-dismiss + close button
 *  - confirm dialog form[data-confirm]    Bootstrap modal instead of confirm()
 *  - live filter    [data-filter]          client-side dish search on the menu
 *
 * No inline handlers anywhere (the CSP forbids inline script).
 */
(function () {
  "use strict";

  var root = document.documentElement;

  // --- Theme ---------------------------------------------------------------
  function syncThemeButtons() {
    var dark = root.getAttribute("data-bs-theme") === "dark";
    document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
      btn.setAttribute("aria-pressed", dark ? "true" : "false");
      btn.setAttribute("title", dark ? "Switch to light theme" : "Switch to dark theme");
      var icon = btn.querySelector(".bi");
      if (icon) {
        icon.className = "bi " + (dark ? "bi-sun" : "bi-moon-stars");
      }
    });
  }

  document.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-theme-toggle]");
    if (!btn) {
      return;
    }
    var next = root.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
    root.setAttribute("data-bs-theme", next);
    try {
      localStorage.setItem("qj-theme", next);
    } catch (e) { /* not persisted; still applied for this page */ }
    syncThemeButtons();
  });

  // --- Toasts --------------------------------------------------------------
  function dismiss(toast) {
    if (!toast || toast.classList.contains("is-leaving")) {
      return;
    }
    toast.classList.add("is-leaving");
    toast.addEventListener("animationend", function () { toast.remove(); }, { once: true });
    // Reduced motion: animations are ~0 ms, but make sure it goes regardless.
    setTimeout(function () { toast.remove(); }, 400);
  }

  function initToasts() {
    document.querySelectorAll(".qj-toast").forEach(function (toast) {
      var timer = setTimeout(function () { dismiss(toast); }, toast.classList.contains("qj-toast--error") ? 9000 : 5000);
      // Pause while the user is reading or has focus inside it.
      toast.addEventListener("mouseenter", function () { clearTimeout(timer); });
      toast.addEventListener("focusin", function () { clearTimeout(timer); });
    });
  }

  document.addEventListener("click", function (event) {
    var close = event.target.closest("[data-toast-close]");
    if (close) {
      dismiss(close.closest(".qj-toast"));
    }
  });

  // --- Confirm dialog -------------------------------------------------------
  // Runs in the capture phase so it sees the submit before loading.js
  // disables the button. After "confirm" the form is re-submitted with the
  // same submitter, so the request is byte-for-byte what it would have been.
  var pending = null;

  window.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.hasAttribute || !form.hasAttribute("data-confirm")) {
      return;
    }
    if (form.dataset.confirmed === "yes") {
      delete form.dataset.confirmed;
      return;
    }
    var modalEl = document.getElementById("qj-confirm");
    if (!modalEl || !window.bootstrap) {
      return; // no dialog available: submit as before
    }
    event.preventDefault();
    event.stopImmediatePropagation();
    pending = { form: form, submitter: event.submitter || null };
    modalEl.querySelector("[data-confirm-title]").textContent = form.getAttribute("data-confirm");
    modalEl.querySelector("[data-confirm-body]").textContent =
      form.getAttribute("data-confirm-body") || "This cannot be undone.";
    modalEl.querySelector("[data-confirm-ok]").textContent = form.getAttribute("data-confirm-ok") || "Confirm";
    modalEl.querySelector("[data-confirm-cancel]").textContent = form.getAttribute("data-confirm-cancel") || "Go back";
    window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
  }, true);

  document.addEventListener("click", function (event) {
    if (!event.target.closest("[data-confirm-ok]") || !pending) {
      return;
    }
    var job = pending;
    pending = null;
    window.bootstrap.Modal.getOrCreateInstance(document.getElementById("qj-confirm")).hide();
    job.form.dataset.confirmed = "yes";
    if (job.form.requestSubmit) {
      job.form.requestSubmit(job.submitter);
    } else {
      job.form.submit();
    }
  });

  // --- Live filter (menu search) --------------------------------------------
  // <input data-filter="#grid"> hides children of #grid whose data-filter-text
  // does not contain the query. Filters what is already on the page; it
  // never requests anything.
  document.addEventListener("input", function (event) {
    var input = event.target;
    if (!input.matches || !input.matches("[data-filter]")) {
      return;
    }
    var grid = document.querySelector(input.getAttribute("data-filter"));
    if (!grid) {
      return;
    }
    var query = input.value.trim().toLowerCase();
    var shown = 0;
    grid.querySelectorAll("[data-filter-text]").forEach(function (item) {
      var match = !query || item.getAttribute("data-filter-text").indexOf(query) !== -1;
      item.hidden = !match;
      shown += match ? 1 : 0;
    });
    var empty = document.querySelector(input.getAttribute("data-filter-empty"));
    if (empty) {
      empty.hidden = shown !== 0;
    }
    var live = document.getElementById("global-status");
    if (live) {
      live.textContent = query ? shown + (shown === 1 ? " dish" : " dishes") + " found" : "";
    }
  });

  syncThemeButtons();
  initToasts();
})();
