/* Dashboard theme: Auto / Light / Dark. Loaded in <head> (before first paint) so there is no dark flash.
   The only thing stored in the browser is the display preference under "primebot.dash.theme" (never a secret). */
(function () {
  "use strict";
  var KEY = "primebot.dash.theme", MODES = ["auto", "light", "dark"], LABEL = { auto: "Auto", light: "Light", dark: "Dark" };
  var root = document.documentElement;
  var mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;

  function read() {
    try { var v = localStorage.getItem(KEY); return MODES.indexOf(v) > 0 ? v : "auto"; } catch (e) { return "auto"; }
  }
  function resolve(mode) { return mode === "auto" ? (mq && mq.matches ? "light" : "dark") : mode; }

  function paint() {
    var mode = read(), eff = resolve(mode);
    root.setAttribute("data-theme", eff);
    root.setAttribute("data-theme-mode", mode);
    var scheme = document.getElementById("metaScheme"), tc = document.getElementById("metaTheme");
    if (scheme) scheme.setAttribute("content", eff);
    if (tc) tc.setAttribute("content", eff === "light" ? "#f3f7fb" : "#020407");
    var b = document.getElementById("themeBtn");
    if (b) { b.textContent = "Theme: " + LABEL[mode]; b.setAttribute("aria-label", "Colour theme: " + LABEL[mode] + ". Activate to change."); }
    try { window.dispatchEvent(new Event("pb-theme")); } catch (e) { /* old browsers */ }
  }
  function set(mode) {
    if (MODES.indexOf(mode) < 0) return;
    try { if (mode === "auto") localStorage.removeItem(KEY); else localStorage.setItem(KEY, mode); } catch (e) { /* storage blocked: still applies for this page view */ }
    paint();
  }
  function next() { var m = read(); set(MODES[(MODES.indexOf(m) + 1) % MODES.length]); }

  window.PBTheme = { get: read, set: set, next: next, resolved: function () { return resolve(read()); } };
  paint();
  if (mq) { var on = function () { if (read() === "auto") paint(); }; if (mq.addEventListener) mq.addEventListener("change", on); else if (mq.addListener) mq.addListener(on); }
  document.addEventListener("DOMContentLoaded", function () {
    var b = document.getElementById("themeBtn");
    if (b) { b.addEventListener("click", next); paint(); }
  });
})();
