/* Applies the saved (or system) colour theme before first paint, so a dark-mode
 * user never sees a white flash. Loaded synchronously in <head>; everything
 * else is in app.js. Without JavaScript the page stays in the light theme. */
(function () {
  "use strict";
  var theme = null;
  try {
    theme = localStorage.getItem("qj-theme");
  } catch (e) { /* storage blocked: fall back to the system preference */ }
  if (theme !== "light" && theme !== "dark") {
    theme = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  document.documentElement.setAttribute("data-bs-theme", theme);
})();
