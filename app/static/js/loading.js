/* Loading feedback for slow navigations.
 *
 * One page in this application is genuinely slow: /recommendations/explain
 * runs the local language model on CPU, which takes a few seconds (and ~18 s
 * if the model has not been warmed up yet). The page is rendered server-side,
 * so by the time it arrives the work is already finished -- which means a
 * spinner *inside* that page would never be seen. The wait happens during
 * navigation, so that is where the feedback has to go.
 *
 * Progressive enhancement: with JavaScript disabled every link still works
 * exactly as before, just without the indicator. Nothing here changes what is
 * submitted or where it goes.
 */
(function () {
  "use strict";

  function markBusy(element) {
    if (element.dataset.loadingActive === "true") {
      return; // already clicked once; don't stack indicators
    }
    element.dataset.loadingActive = "true";

    var message = element.dataset.loading || "Working…";

    // Keep the width stable so the layout does not jump.
    element.style.minWidth = element.offsetWidth + "px";
    element.setAttribute("aria-busy", "true");
    element.classList.add("disabled");
    element.innerHTML =
      '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>' +
      "<span>" + message + "</span>";

    // Announce to assistive technology, which will not notice innerHTML alone.
    var live = document.getElementById("global-status");
    if (live) {
      live.textContent = message;
    }
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-loading]");
    if (!trigger) {
      return;
    }
    // Let modified clicks (new tab, download) behave normally.
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) {
      return;
    }
    markBusy(trigger);
  });
})();
