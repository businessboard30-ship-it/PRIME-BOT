/* Owner area (Phase 0 shell). Lazy-loaded by dash.js for #/owner/<section>.
   Holds no secrets. Every call is gated on the server; owner_sections only decides which pages to show.
   Server data is only ever written with textContent (via h(..., {text}) or string children). */
(function () {
  "use strict";
  var H = window.DashOwnerHost, S = H.S, api = H.api, h = H.h, app = H.app, toast = H.toast;

  // Phase 0 pages. Later phases add entries here; each needs the matching server-side section.
  var PAGES = [
    { id: "", label: "Overview", need: null },
    { id: "health", label: "Health", need: "health" },
    { id: "servers", label: "Servers", need: "servers" },
    { id: "users", label: "Users", need: "inspect" },
    { id: "payments", label: "Payments", need: "money" },
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


  /* ---------- shared bits for read-only pages ---------- */
  function table(headers, rows) {
    return h("div", { class: "owner-scroll" }, h("table", { class: "owner-table" },
      h("thead", null, h("tr", null, headers.map(function (t) { return h("th", { text: t }); }))),
      h("tbody", null, rows.map(function (r) { return h("tr", null, r.map(function (v) { return h("td", { text: String(v == null ? "" : v) }); })); }))));
  }
  function loadInto(box, action, params, draw) {
    box.textContent = ""; box.appendChild(h("p", { class: "muted", text: "Loading" }));
    api(action, params || {}).then(function (j) { box.textContent = ""; draw(j); })
      .catch(function (e) { if (e.message !== "401") { box.textContent = ""; box.appendChild(h("p", { class: "muted", text: e.message })); } });
  }
  function kv(pairs) {
    return h("dl", { class: "owner-kv" }, pairs.map(function (p) { return [h("dt", { text: p[0] }), h("dd", { text: String(p[1] == null ? "n/a" : p[1]) })]; }));
  }
  function money(n) { return (Math.round((n || 0) * 100) / 100).toFixed(2); }

  function health() {
    var box = h("div", { class: "card" }); shell("health", box);
    loadInto(box, "owner_health", {}, function (j) {
      var sv = j.servers || {};
      box.appendChild(kv([["Database round trip", j.db_ping_ms == null ? "unreachable" : j.db_ping_ms + " ms"],
        ["Servers (main bot)", sv.main], ["Servers (clones)", sv.clones], ["Active clones", j.clones_active],
        ["Quiet clones", j.clones_quiet == null ? "unknown" : j.clones_quiet.length], ["As of", fmt(j.as_of)]]));
      box.appendChild(h("p", { class: "muted", text: "Live bot stats (uptime, latency, error counts) are not available on the web yet. They need the worker to publish a snapshot." }));
      if (j.clones_quiet && j.clones_quiet.length) {
        box.appendChild(h("h3", { text: "Clones with no heartbeat in " + j.heartbeat_stale_minutes + " minutes" }));
        box.appendChild(table(["Clone", "Bot", "Last heartbeat"], j.clones_quiet.map(function (c) { return [c.clone_id, c.bot_username, c.last_heartbeat ? fmt(c.last_heartbeat) : "never"]; })));
      }
    });
  }

  function servers() {
    var page = 0, q = h("input", { type: "search", placeholder: "Name or server id", "aria-label": "Search servers" });
    var which = h("select", { "aria-label": "Which bot" }, h("option", { value: "", text: "All bots" }), h("option", { value: "main", text: "Main bot only" }));
    var left = h("input", { type: "checkbox", id: "own-left" });
    var out = h("div", null), detail = h("div", { class: "card" });
    function go() {
      loadInto(out, "owner_servers", { q: q.value, clone: which.value, active: left.checked ? "0" : "1", page: page }, function (j) {
        out.appendChild(table(["Server", "Id", "Members", "Bot", "Joined", "Left"], j.rows.map(function (r) {
          return [r.guild_name, r.guild_id, r.member_count, r.clone_id ? "clone " + r.clone_id + (r.bot_username ? " (" + r.bot_username + ")" : "") : "main", fmt(r.joined_at), r.left_at ? fmt(r.left_at) : ""];
        })));
        var last = Math.max(0, Math.ceil(j.total / j.per_page) - 1);
        out.appendChild(h("p", { class: "muted", text: j.total + " servers. Page " + (page + 1) + " of " + (last + 1) + "." }));
        out.appendChild(h("button", { class: "btn sm ghost", text: "Previous", disabled: page <= 0, onclick: function () { page--; go(); } }));
        out.appendChild(h("button", { class: "btn sm ghost", text: "Next", disabled: page >= last, onclick: function () { page++; go(); } }));
      });
    }
    function inspect() {
      var id = q.value.trim();
      if (!/^\d{10,20}$/.test(id)) { toast("Type a full server id to inspect it.", "bad"); return; }
      loadInto(detail, "owner_server", { guild_id: id }, function (j) {
        detail.appendChild(h("h3", { text: "Server " + j.guild_id }));
        detail.appendChild(table(["Bot", "Name", "Members", "Owner", "Joined", "Left"], j.bots.map(function (r) {
          return [r.clone_id ? "clone " + r.clone_id : "main", r.guild_name, r.member_count, r.owner_id, fmt(r.joined_at), r.left_at ? fmt(r.left_at) : ""]; })));
        detail.appendChild(kv([["Blocked", j.blocked ? "yes: " + (j.blocked.reason || "no reason") : "no"],
          ["Premium", (j.premium || []).map(function (p) { return (p.clone_id ? "clone " + p.clone_id : "main") + " until " + fmt(p.expires_at); }).join("; ") || "none"],
          ["Reports", Object.keys(j.reports || {}).map(function (k) { return k + ": " + j.reports[k]; }).join(", ") || "none"]]));
      });
    }
    shell("servers", h("div", null, h("div", { class: "owner-bar" }, q, which, h("label", null, left, " include left"),
      h("button", { class: "btn sm", text: "Search", onclick: function () { page = 0; go(); } }),
      h("button", { class: "btn sm ghost", text: "Inspect id", onclick: inspect })), out, detail));
    go();
  }

  function users() {
    var id = h("input", { type: "text", placeholder: "User id", "aria-label": "User id", inputmode: "numeric" }), out = h("div", { class: "card" });
    function look() {
      if (!/^\d{10,20}$/.test(id.value.trim())) { toast("Type a full user id.", "bad"); return; }
      loadInto(out, "owner_user", { user_id: id.value.trim() }, function (u) {
        out.appendChild(kv([["User", (u.username || "unknown") + " (" + u.user_id + ")"],
          ["Blocked", u.blocked ? "yes: " + (u.blocked.reason || "no reason") : "no"],
          ["Coins total", u.coins_total + " across " + u.coins_servers + " servers"], ["XP in", u.xp_servers + " servers"]]));
        if (u.owned.length) out.appendChild(table(["Owns server", "Id", "Bot"], u.owned.map(function (g) { return [g.guild_name, g.guild_id, g.clone_id ? "clone " + g.clone_id : "main"]; })));
        if (u.clones.length) out.appendChild(table(["Clone", "Bot", "Status"], u.clones.map(function (c) { return [c.clone_id, c.bot_username, c.status]; })));
        if (u.payments.length) out.appendChild(table(["Payments", "Count", "Total"], u.payments.map(function (p) { return [p.status, p.n, money(p.total)]; })));
      });
    }
    out.appendChild(h("p", { class: "muted", text: "Look up a user by id." }));
    shell("users", h("div", null, h("div", { class: "owner-bar" }, id, h("button", { class: "btn sm", text: "Look up", onclick: look })), out));
  }

  function payments() {
    var view = "pending", out = h("div", { class: "card" }), nav = h("div", { class: "owner-bar" });
    function tab(id, label) { return h("button", { class: "btn sm " + (view === id ? "primary" : "ghost"), text: label, onclick: function () { view = id; draw(); } }); }
    function draw() {
      nav.textContent = ""; [tab("pending", "Pending"), tab("failures", "Failed"), tab("revenue", "Revenue"), tab("expiries", "Expiries")].forEach(function (b) { nav.appendChild(b); });
      if (view === "expiries") return loadInto(out, "owner_expiries", { days: 7 }, function (j) {
        out.appendChild(j.rows.length ? table(["Server", "Bot", "Expires", "Auto-renews"], j.rows.map(function (r) { return [r.guild_id, r.clone_id ? "clone " + r.clone_id : "main", fmt(r.expires_at), r.auto_renews ? "yes" : "no"]; })) : h("p", { class: "muted", text: "Nothing expires in the next 7 days." }));
      });
      loadInto(out, "owner_payments", { view: view }, function (j) {
        if (view === "pending") {
          out.appendChild(h("p", { class: "muted", text: j.total + " pending (showing " + j.rows.length + "). Read-only; approve and reverse stay in Discord for now." }));
          out.appendChild(table(["Id", "Reference", "User", "Amount", "Type", "Provider", "Created"], j.rows.map(function (r) { return [r.payment_id, r.paystack_reference, r.user_id, money(r.amount), r.payment_type, r.provider, fmt(r.created_date)]; })));
        } else if (view === "failures") {
          out.appendChild(j.rows.length ? table(["When", "Source", "Kind", "Reference", "Detail"], j.rows.map(function (r) { return [fmt(r.created_at), r.source, r.kind, r.reference, r.detail]; })) : h("p", { class: "muted", text: "No open payment failures." }));
        } else {
          ["GHS", "USD"].forEach(function (cur) {
            var rows = j.series[cur] || [], sum = rows.reduce(function (a, r) { return a + r.total; }, 0);
            out.appendChild(h("h3", { text: cur + ": " + money(sum) + " over " + rows.length + " days" }));
            out.appendChild(table(["Day", "Total", "Payments"], rows.slice().reverse().map(function (r) { return [fmt(r.start).split(",")[0], money(r.total), r.count]; })));
          });
        }
      });
    }
    shell("payments", h("div", null, nav, out)); draw();
  }

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
      ({ "": overview, health: health, servers: servers, users: users, payments: payments, audit: audit, security: security })[page.id]();
    }
  };
})();
