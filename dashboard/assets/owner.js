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
    { id: "controls", label: "Controls", need: "controls" },
    { id: "blacklist", label: "Blacklist", need: "blacklist" },
    { id: "premium", label: "Premium", need: "premium" },
    { id: "announce", label: "Announcements", need: "broadcast" },
    { id: "feedback", label: "Feedback", need: "feedback" },
    { id: "logs", label: "Logs", need: "logs" },
    { id: "config", label: "Config", need: "config" },
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
      var lb = j.live_bot;
      if (!lb) { box.appendChild(h("p", { class: "muted", text: "No live bot snapshot yet. The bot worker publishes one about every minute once it is running the latest version." })); }
      else {
        box.appendChild(h("h3", { text: "Live bot (as of " + lb.age_s + " s ago)" }));
        if (lb.stale) box.appendChild(h("p", { class: "owner-warn", text: "This snapshot is old. The bot worker may be down or stuck." }));
        box.appendChild(kv([["Uptime", lb.uptime], ["Gateway latency", lb.latency_ms == null ? "n/a" : lb.latency_ms + " ms"],
          ["Memory", lb.memory_mb == null ? "n/a" : Math.round(lb.memory_mb) + " MB"], ["Servers", lb.servers], ["Cogs", lb.cogs],
          ["Loops running", lb.loops_running], ["Stopped loops", (lb.loops_stopped || []).join(", ") || "none"],
          ["Errors (last " + Math.round(lb.window_s / 60) + " min)", lb.errors_window], ["Warnings", lb.warnings_window]]));
      }
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

  /* ---------- Phase 2: safe controls ---------- */
  function typed(word, what) {
    var v = window.prompt(what + "\n\nType " + word + " to confirm.");
    return v === null ? null : v;
  }
  function fail(e) {
    if (e.message === "401") return;
    toast(e.payload && e.payload.code === "stepup_required" ? "Confirm it's you first: Security page, then try again." : e.message, "bad");
  }
  function age(j) { return j.available === false ? "The bot worker has not published this yet." : "Snapshot is " + j.age_s + " s old" + (j.stale ? " (stale: the worker may be down)." : "."); }

  function controls() {
    var box = h("div", { class: "card" }); shell("controls", box);
    function draw() {
      loadInto(box, "owner_controls", {}, function (j) {
        box.appendChild(h("p", { class: "muted", text: j.note }));
        j.switches.forEach(function (sw) {
          var on = sw.engaged, offLabel = sw.kind === "opt_in" ? (on ? "ON for everyone" : "owner only") : (on ? "TURNED OFF" : "running");
          box.appendChild(h("div", { class: "owner-bar" }, h("strong", { text: sw.label }), h("span", { class: "muted", text: offLabel }),
            h("button", { class: "btn sm " + (on ? "primary" : "ghost"), text: on ? "Release" : "Engage", onclick: function () {
              var body = { switch: sw.key, engaged: !on };
              if (!on && sw.kind === "maintenance") { var c = typed("MAINTENANCE", "This blocks every command for everyone."); if (c === null) return; body.confirm = c; }
              if (!on && sw.kind === "opt_in") { var c2 = typed("OPEN", "This opens the feature to everyone."); if (c2 === null) return; body.confirm = c2; }
              api("owner_switch", {}, body).then(function () { toast("Saved.", "ok"); draw(); }).catch(fail);
            } })));
        });
      });
    }
    draw();
  }

  function blacklist() {
    var kind = h("select", { "aria-label": "Kind" }, h("option", { value: "user", text: "User" }), h("option", { value: "guild", text: "Server" }));
    var id = h("input", { type: "text", placeholder: "Full id", inputmode: "numeric", "aria-label": "Id" });
    var why = h("input", { type: "text", placeholder: "Reason (optional)", maxlength: "300", "aria-label": "Reason" });
    var out = h("div", null);
    function draw() {
      loadInto(out, "owner_blacklist", {}, function (j) {
        out.appendChild(j.rows.length ? table(["Kind", "Id", "Reason", "Added", ""], j.rows.map(function (r) { return [r.kind, r.target_id, r.reason, fmt(r.created_at), ""]; })) : h("p", { class: "muted", text: "Nobody is blacklisted." }));
        j.rows.forEach(function (r) {
          out.appendChild(h("button", { class: "btn sm ghost", text: "Remove " + r.kind + " " + r.target_id, onclick: function () {
            api("owner_blacklist_remove", {}, { kind: r.kind, target_id: r.target_id }).then(function () { toast("Removed.", "ok"); draw(); }).catch(fail);
          } }));
        });
      });
    }
    shell("blacklist", h("div", null, h("div", { class: "owner-bar" }, kind, id, why, h("button", { class: "btn sm", text: "Add", onclick: function () {
      api("owner_blacklist_add", {}, { kind: kind.value, target_id: id.value.trim(), reason: why.value }).then(function () { id.value = ""; why.value = ""; toast("Added.", "ok"); draw(); }).catch(fail);
    } })), out));
    draw();
  }

  function premium() {
    var out = h("div", { class: "card" }), gid = h("input", { type: "text", placeholder: "Server id", inputmode: "numeric", "aria-label": "Server id" });
    var clone = h("input", { type: "text", placeholder: "Clone # (blank = main)", inputmode: "numeric", "aria-label": "Clone number" });
    function draw() {
      loadInto(out, "owner_premium", {}, function (j) {
        out.appendChild(h("p", { class: "muted", text: "Soonest to expire first. Revoking ends Premium immediately (step-up + typed REVOKE). Granting stays in Discord for now." }));
        out.appendChild(table(["Server", "Name", "Bot", "Expires"], j.rows.map(function (r) { return [r.guild_id, r.guild_name, r.clone_id ? "clone " + r.clone_id : "main", fmt(r.expires_at)]; })));
      });
    }
    shell("premium", h("div", null, h("div", { class: "owner-bar" }, gid, clone, h("button", { class: "btn sm", text: "Revoke Premium", onclick: function () {
      var c = typed("REVOKE", "End Premium for server " + gid.value.trim() + " now."); if (c === null) return;
      api("owner_premium_revoke", {}, { guild_id: gid.value.trim(), clone: clone.value.trim(), confirm: c }).then(function (j) { toast(j.revoked ? "Premium revoked." : "No subscription found.", j.revoked ? "ok" : "bad"); draw(); }).catch(fail);
    } })), out));
    draw();
  }

  function announce() {
    var title = h("input", { type: "text", placeholder: "Title", maxlength: "100", "aria-label": "Title" });
    var body = h("textarea", { placeholder: "Message", rows: "5", maxlength: "2000", "aria-label": "Message" });
    var push = h("input", { type: "checkbox", id: "own-push" });
    var out = h("div", null);
    function draw() {
      loadInto(out, "dropbox", {}, function (j) {
        out.appendChild(j.messages.length ? table(["Id", "Title", "Sent", ""], j.messages.map(function (m) { return [m.id, m.title, fmt(m.created_at), ""]; })) : h("p", { class: "muted", text: "Nothing sent yet." }));
        j.messages.forEach(function (m) {
          out.appendChild(h("button", { class: "btn sm ghost", text: "Delete " + m.id, onclick: function () {
            api("owner_announce_delete", {}, { id: m.id }).then(function () { toast("Deleted.", "ok"); draw(); }).catch(fail);
          } }));
        });
      });
    }
    shell("announce", h("div", null, h("div", { class: "card" },
      h("p", { class: "muted", text: "Posts to every dashboard admin's inbox. Ticking DM also messages server owners: that needs a step-up and typed SEND." }),
      title, body, h("label", null, push, " Also DM server owners"),
      h("button", { class: "btn", text: "Send", onclick: function () {
        var b = { title: title.value, body: body.value, kind: "info", announce: true, audience: "all", push_dm: push.checked };
        if (push.checked) { var c = typed("SEND", "This DMs server owners."); if (c === null) return; b.confirm = c; }
        api("owner_announce", {}, b).then(function (j) { title.value = ""; body.value = ""; toast("Sent (" + j.recipients + " DMs queued).", "ok"); draw(); }).catch(fail);
      } })), out));
    draw();
  }

  function feedback() {
    var box = h("div", { class: "card" }); shell("feedback", box);
    loadInto(box, "owner_feedback", {}, function (j) {
      box.appendChild(j.rows.length ? table(["When", "User", "Server", "Message"], j.rows.map(function (r) { return [fmt(r.created_at), r.user_id, r.guild_id, r.message]; })) : h("p", { class: "muted", text: "No feedback yet." }));
    });
  }

  function logs() {
    var level = h("select", { "aria-label": "Level" }, h("option", { value: "", text: "Warnings and errors" }), h("option", { value: "ERROR", text: "Errors only" }));
    var out = h("div", null);
    function draw() {
      loadInto(out, "owner_logs", { level: level.value }, function (j) {
        out.appendChild(h("p", { class: "muted", text: age(j) + " Secrets are masked by the bot before they are published." }));
        out.appendChild(j.lines.length ? table(["When", "Level", "Logger", "Message"], j.lines.map(function (l) { return [new Date(l.ts * 1000).toLocaleString(), l.level, l.logger, l.message]; })) : h("p", { class: "muted", text: "No recent warnings." }));
      });
    }
    level.onchange = draw;
    shell("logs", h("div", null, h("div", { class: "owner-bar" }, level, h("button", { class: "btn sm ghost", text: "Refresh", onclick: draw })), h("div", { class: "card" }, out)));
    draw();
  }

  function config() {
    var q = h("input", { type: "search", placeholder: "Filter by name", "aria-label": "Filter settings" }), out = h("div", null);
    function draw() {
      loadInto(out, "owner_config", { q: q.value }, function (j) {
        out.appendChild(h("p", { class: "muted", text: age(j) + " Values come from the bot worker. Secrets only show set or not set." }));
        out.appendChild(table(["Setting", "Value"], j.entries.map(function (e) { return [e.name, e.shown]; })));
      });
    }
    shell("config", h("div", null, h("div", { class: "owner-bar" }, q, h("button", { class: "btn sm", text: "Filter", onclick: draw })), h("div", { class: "card" }, out)));
    draw();
  }

  window.DashOwner = {
    render: function (sub) {
      var page = PAGES.filter(function (p) { return p.id === (sub || "") && allowed(p); })[0];
      if (!page) { location.hash = "#/owner"; return; }
      ({ "": overview, health: health, servers: servers, users: users, payments: payments, audit: audit, security: security,
        controls: controls, blacklist: blacklist, premium: premium, announce: announce, feedback: feedback, logs: logs, config: config })[page.id]();
    }
  };
})();
