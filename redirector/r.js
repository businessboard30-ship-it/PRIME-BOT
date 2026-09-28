/* Stable-URL redirector. Each page declares <meta name="target" content="backend:/api/x">
   (or "dashboard:/manual") and <meta name="config" content="../config.json"> (relative to the
   page). This loads config.json (the ONE place the real hosts live) and forwards the browser
   there, keeping ?query and #hash intact. Targets are fixed per page, so it is not an open redirect. */
(function (root) {
  function buildTarget(cfg, target, search, hash) {
    var m = /^(backend|dashboard):(\/[^\s]*)$/.exec(target || "");
    if (!m) throw new Error("bad target");
    var base = String(cfg[m[1] + "_url"] || "").replace(/\/+$/, "");
    if (!/^https:\/\/[^\s\/?#]+(\/[^\s?#]*)?$/.test(base)) throw new Error("bad base url");
    return base + m[2] + (search || "") + (hash || "");
  }
  root.buildTarget = buildTarget;
  if (typeof document === "undefined") return;
  var meta = function (n) { var e = document.querySelector('meta[name="' + n + '"]'); return e ? e.content : ""; };
  fetch(new URL(meta("config"), location.href).href, { cache: "no-store" })
    .then(function (r) { if (!r.ok) throw new Error("config"); return r.json(); })
    .then(function (cfg) { location.replace(buildTarget(cfg, meta("target"), location.search, location.hash)); })
    .catch(function () {
      var box = document.getElementById("msg");
      if (box) box.textContent = "Couldn't redirect right now. Please try again in a moment.";
    });
})(typeof window !== "undefined" ? window : globalThis);
