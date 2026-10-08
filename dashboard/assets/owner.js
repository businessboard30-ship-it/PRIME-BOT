/* Owner area (Phase 0 shell). Lazy-loaded by dash.js for #/owner/<section>.
   Holds no secrets. Every call is gated on the server; owner_sections only decides which pages to show.
   Server data is only ever written with textContent (via h(..., {text}) or string children). */
(function () {
  "use strict";
  var H = window.DashOwnerHost, S = H.S, api = H.api, h = H.h, app = H.app, toast = H.toast;

  // Phase 0 pages. Later phases add entries here; each needs the matching server-side section.
  var PAGES = [
    { id: "", label: "Overview", need: null },
    { id: "audit", label: "Audit log", need: "audit" },
    { id: "security", label: "Security", need: "controls" }
  ];
  function allowed(p) { return !p.need || (S.ownerSections || []).indexOf(p.need) !== -1; }

  function shell(current, content) {
    var nav = h("nav", { class: "owner-nav", "aria-label": "Owner sections" });
    PAGES.filter(allowed).forEach(function (p) {
      nav.appendChild(h("a", { class: "btn sm " + (p.id === current ? "primary" : "ghost"), href: "#/owner" + (p.id ? "/" + p.id : ""), text: p.label }));
    });
    var main = h("main", { id: "main", class: "owner-main" }, h("h1", { text: "Owner" }), nav, content);
    app.textContent = ""; app.appendChild(main); window.scrollTo(0, 0);
  }

  function overview() {
    var sections = (S.ownerSections || []).slice().sort();
    shell("", h("div", { class: "card" },
      h("p", { text: "Signed in as " + (S.user && S.user.username || "owner") + ". Owner sessions last about 2 hours." }),
      h("p", { class: "muted", text: "Pages appear here as they ship. Access is decided by the server, not this page." }),
      h("p", { class: "muted mono", text: "Sections: " + (sections.join(", ") || "none") })));
  }

  function fmt(iso) { try { return new Date(iso).toLocaleString(); } catch (e) { return String(iso); } }

  function audit() {
    var box = h("div", { class: "card" }, h("p", { class: "muted", text: "Loading" }));
    shell("audit", box);
    api("owner_audit").then(function (j) {
      box.textContent = "";
      if (!j.entries.length) { box.appendChild(h("p", { class: "muted", text: "No owner actions recorded yet." })); return; }
      var tbl = h("table", { class: "owner-table" },
        h("thead", null, h("tr", null, ["When", "Who", "Source", "Section", "Action", "Target", "Result"].map(function (t) { return h("th", { text: t }); }))),
        h("tbody", null, j.entries.map(function (r) {
          return h("tr", null, [fmt(r.created_at), r.user_name || r.user_id, r.source, r.section, r.action, r.target, r.result].map(function (v) { return h("td", { text: String(v == null ? "" : v) }); }));
        })));
      box.appendChild(h("div", { class: "owner-scroll" }, tbl));
    }).catch(function (e) { if (e.message !== "401") { box.textContent = ""; box.appendChild(h("p", { class: "muted", text: e.message })); } });
  }

  function security() {
    var note = h("p", { class: "muted", text: S.stepupDone ? "Confirmed. Destructive actions are unlocked for a few minutes." : "Destructive actions need a fresh Discord sign-in (valid for a few minutes) and a typed confirmation." });
    S.stepupDone = false;
    var confirmBtn = h("button", { class: "btn", text: "Confirm it's me (sign in with Discord)", onclick: function () {
      confirmBtn.disabled = true;
      api("owner_stepup", {}, {}).then(function (j) { location.href = j.url; })
        .catch(function (e) { confirmBtn.disabled = false; if (e.message !== "401") toast(e.message, "bad"); });
    } });
    var typed = h("input", { type: "text", placeholder: "Type SIGN OUT", autocomplete: "off", "aria-label": "Type SIGN OUT to confirm" });
    var outBtn = h("button", { class: "btn", text: "Sign out everywhere", onclick: function () {
      outBtn.disabled = true;
      api("owner_signout_all", {}, { confirm: typed.value }).then(function () {
        localStorage.removeItem("primebot.dash.session"); location.hash = "#/"; location.reload();
      }).catch(function (e) {
        outBtn.disabled = false;
        if (e.message === "401") return;
        toast(e.payload && e.payload.code === "stepup_required" ? "Confirm it's you first (button above)." : e.message, "bad");
      });
    } });
    shell("security", h("div", { class: "card" }, h("h3", { text: "Step-up sign-in" }), note, confirmBtn,
      h("h3", { text: "Sign out everywhere" }),
      h("p", { class: "muted", text: "Ends every dashboard session for your account, on every device." }), typed, outBtn));
  }

  window.DashOwner = {
    render: function (sub) {
      var page = PAGES.filter(function (p) { return p.id === (sub || "") && allowed(p); })[0];
      if (!page) { location.hash = "#/owner"; return; }
      ({ "": overview, audit: audit, security: security })[page.id]();
    }
  };
})();
