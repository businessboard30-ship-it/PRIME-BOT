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
    { id: "ads", label: "Ads and Marketplace", need: "ads" },
    { id: "bump", label: "Bump network", need: "bump" },
    { id: "watchlist", label: "Abuse watchlist", need: "watchlist" },
    { id: "reports", label: "Report queue", need: "reports" },
    { id: "status", label: "Bot status", need: "status" },
    { id: "honeypot", label: "Honeypots", need: "honeypot" },
    { id: "scamshield", label: "Scam Shield", need: "scamshield" },
    { id: "helpers", label: "Helpers", need: "access" },
    { id: "clones", label: "Clones", need: "servers" },
    { id: "database", label: "Database", need: "database" },
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
    function drawReverse() {
      out.textContent = "";
      var ref = h("input", { type: "text", placeholder: "Payment reference", "aria-label": "Payment reference" }), res = h("div", null);
      function look() {
        loadInto(res, "owner_payment", { reference: ref.value.trim() }, function (j) {
          var p = j.payment;
          res.appendChild(kv([["Payment", p.payment_id], ["User", p.user_id], ["Amount", money(p.amount)], ["Type", p.payment_type], ["Status", p.status], ["Server", p.chat_id], ["Created", fmt(p.created_date)]]));
          if (!j.can_reverse) { res.appendChild(h("p", { class: "muted", text: j.problem })); return; }
          res.appendChild(h("button", { class: "btn", text: "Reverse this payment", onclick: function () {
            var c = typed("REVERSE", "This takes the premium days back off the server and marks the payment reversed. Refund the money at the gateway yourself."); if (c === null) return;
            api("owner_payment_reverse", {}, { reference: p.paystack_reference, confirm: c }).then(function (r) { toast(r.message, r.changed ? "ok" : "bad"); look(); }).catch(fail);
          } }));
        });
      }
      out.appendChild(h("div", { class: "owner-bar" }, ref, h("button", { class: "btn sm", text: "Look up", onclick: look })));
      out.appendChild(h("p", { class: "muted", text: "Only completed Premium payments can be reversed. Needs a fresh Discord sign-in and typing REVERSE." }));
      out.appendChild(res);
    }
    function drawCoupons() {
      var code = h("input", { type: "text", placeholder: "CODE", maxlength: "24", "aria-label": "Code" });
      var pct = h("input", { type: "text", placeholder: "% off", inputmode: "numeric", "aria-label": "Percent off" });
      var uses = h("input", { type: "text", placeholder: "Max uses (blank = unlimited)", inputmode: "numeric", "aria-label": "Max uses" });
      var days = h("input", { type: "text", placeholder: "Valid days (blank = never expires)", inputmode: "numeric", "aria-label": "Valid days" });
      var list = h("div", null);
      function reload() {
        loadInto(list, "owner_coupons", {}, function (j) {
          list.appendChild(j.rows.length ? table(["Code", "% off", "Uses", "Expires", "State"], j.rows.map(function (c) { return [c.code, c.percent_off, c.uses + (c.max_uses == null ? "" : "/" + c.max_uses), c.expires_at ? fmt(c.expires_at) : "never", c.state]; })) : h("p", { class: "muted", text: "No codes yet." }));
          j.rows.forEach(function (c) {
            var on = c.state !== "disabled";
            list.appendChild(h("button", { class: "btn sm ghost", text: (on ? "Disable " : "Enable ") + c.code, onclick: function () {
              api("owner_coupon_toggle", {}, { code: c.code, active: !on }).then(function () { reload(); }).catch(fail);
            } }));
          });
        });
      }
      out.textContent = "";
      out.appendChild(h("p", { class: "muted", text: "Codes of 50% or more need a fresh Discord sign-in and typing CREATE." }));
      out.appendChild(h("div", { class: "owner-bar" }, code, pct, uses, days, h("button", { class: "btn sm", text: "Create", onclick: function () {
        var b = { code: code.value, percent: pct.value, max_uses: uses.value, days: days.value };
        if (parseInt(pct.value, 10) >= 50) { var c = typed("CREATE", "A " + pct.value + "% discount."); if (c === null) return; b.confirm = c; }
        api("owner_coupon_create", {}, b).then(function (r) { toast(r.created ? "Created " + r.code : "That code already exists.", r.created ? "ok" : "bad"); reload(); }).catch(fail);
      } })));
      out.appendChild(list); reload();
    }
    function draw() {
      nav.textContent = ""; [tab("pending", "Pending"), tab("failures", "Failed"), tab("revenue", "Revenue"), tab("expiries", "Expiries"), tab("reverse", "Reverse"), tab("coupons", "Coupons")].forEach(function (b) { nav.appendChild(b); });
      if (view === "reverse") return drawReverse();
      if (view === "coupons") return drawCoupons();
      if (view === "expiries") return loadInto(out, "owner_expiries", { days: 7 }, function (j) {
        out.appendChild(j.rows.length ? table(["Server", "Bot", "Expires", "Auto-renews"], j.rows.map(function (r) { return [r.guild_id, r.clone_id ? "clone " + r.clone_id : "main", fmt(r.expires_at), r.auto_renews ? "yes" : "no"]; })) : h("p", { class: "muted", text: "Nothing expires in the next 7 days." }));
      });
      loadInto(out, "owner_payments", { view: view }, function (j) {
        if (view === "pending") {
          out.appendChild(h("p", { class: "muted", text: j.total + " pending (showing " + j.rows.length + "). Approve and reject stay in Discord for now." }));
          out.appendChild(table(["Id", "Reference", "User", "Amount", "Type", "Provider", "Created"], j.rows.map(function (r) { return [r.payment_id, r.paystack_reference, r.user_id, money(r.amount), r.payment_type, r.provider, fmt(r.created_date)]; })));
          out.appendChild(h("button", { class: "btn sm ghost", text: "Clear abandoned checkouts (older than 6 h)", onclick: function () {
            var c = typed("CLEAR", "Marks old pending checkouts as expired. Rows are kept."); if (c === null) return;
            api("owner_pending_clear", {}, { confirm: c }).then(function (r) { toast("Expired " + r.expired + " checkout(s).", "ok"); draw(); }).catch(fail);
          } }));
        } else if (view === "failures") {
          out.appendChild(j.rows.length ? table(["When", "Source", "Kind", "Reference", "Detail"], j.rows.map(function (r) { return [fmt(r.created_at), r.source, r.kind, r.reference, r.detail]; })) : h("p", { class: "muted", text: "No open payment failures." }));
          j.rows.forEach(function (r) {
            out.appendChild(h("button", { class: "btn sm ghost", text: "Dismiss " + r.id, onclick: function () {
              api("owner_failure_dismiss", {}, { id: String(r.id) }).then(function () { draw(); }).catch(fail);
            } }));
          });
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
      if (!j.entries.length) { box.appendChild(h("p", { class: "muted", text: "No owner actions recorded yet." })); botAudit(box); return; }
      var tbl = h("table", { class: "owner-table" },
        h("thead", null, h("tr", null, ["When", "Who", "Source", "Section", "Action", "Target", "Result"].map(function (t) { return h("th", { text: t }); }))),
        h("tbody", null, j.entries.map(function (r) {
          return h("tr", null, [fmt(r.created_at), r.user_name || r.user_id, r.source, r.section, r.action, r.target, r.result].map(function (v) { return h("td", { text: String(v == null ? "" : v) }); }));
        })));
      box.appendChild(h("div", { class: "owner-scroll" }, tbl));
      botAudit(box);
    }).catch(function (e) { if (e.message !== "401") { box.textContent = ""; box.appendChild(h("p", { class: "muted", text: e.message })); } });
  }

  function botAudit(into) {
    var act = h("input", { type: "text", placeholder: "Action, e.g. premium.grant", "aria-label": "Action" });
    var gid = h("input", { type: "text", placeholder: "Server id", inputmode: "numeric", "aria-label": "Server id" });
    var adm = h("input", { type: "text", placeholder: "Admin id", inputmode: "numeric", "aria-label": "Admin id" });
    var out = h("div", null);
    function go() {
      loadInto(out, "owner_botaudit", { what: act.value.trim(), guild_id: gid.value.trim(), admin_id: adm.value.trim() }, function (j) {
        out.appendChild(j.rows.length ? table(["When", "Admin", "Action", "Server", "Details"], j.rows.map(function (r) { return [fmt(r.created_at), r.admin_id, r.action, r.guild_id, r.details]; })) : h("p", { class: "muted", text: "No matching entries." }));
      });
    }
    into.appendChild(h("h3", { text: "Discord panel audit (the bot's own log)" }));
    into.appendChild(h("div", { class: "owner-bar" }, act, gid, adm, h("button", { class: "btn sm", text: "Filter", onclick: go })));
    into.appendChild(out); go();
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
      var bl = { kind: kind.value, target_id: id.value.trim(), reason: why.value };
      if (kind.value === "guild") { var cb = typed("BLOCK", "This cuts server " + bl.target_id + " off from the bot."); if (cb === null) return; bl.confirm = cb; }
      api("owner_blacklist_add", {}, bl).then(function () { id.value = ""; why.value = ""; toast("Added.", "ok"); draw(); }).catch(fail);
    } })), out));
    draw();
  }

  function premium() {
    var out = h("div", { class: "card" }), gid = h("input", { type: "text", placeholder: "Server id", inputmode: "numeric", "aria-label": "Server id" });
    var clone = h("input", { type: "text", placeholder: "Clone # (blank = main)", inputmode: "numeric", "aria-label": "Clone number" });
    var days = h("input", { type: "text", placeholder: "Days to add", inputmode: "numeric", "aria-label": "Days to add" });
    function draw() {
      loadInto(out, "owner_premium", {}, function (j) {
        out.appendChild(h("p", { class: "muted", text: "Soonest to expire first. Grant adds days on top of what is left (step-up + typed GRANT). Revoke ends Premium immediately (step-up + typed REVOKE)." }));
        out.appendChild(table(["Server", "Name", "Bot", "Expires"], j.rows.map(function (r) { return [r.guild_id, r.guild_name, r.clone_id ? "clone " + r.clone_id : "main", fmt(r.expires_at)]; })));
      });
    }
    shell("premium", h("div", null, h("div", { class: "owner-bar" }, gid, clone, days, h("button", { class: "btn sm", text: "Grant / extend", onclick: function () {
      var c = typed("GRANT", "Add " + days.value.trim() + " day(s) of Premium to server " + gid.value.trim() + "."); if (c === null) return;
      api("owner_premium_grant", {}, { guild_id: gid.value.trim(), clone: clone.value.trim(), days: days.value.trim(), confirm: c }).then(function (j) { toast("Premium now runs until " + fmt(j.expires_at) + ".", "ok"); draw(); }).catch(fail);
    } }), h("button", { class: "btn sm ghost", text: "Revoke Premium", onclick: function () {
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

  /* ---------- Phase 3 remainder: Ads, Marketplace, Bump ---------- */
  function ads() {
    var view = "ads", flt = "pending", page = 0;
    var nav = h("div", { class: "owner-bar" }), out = h("div", null);
    function tab(id, label) { return h("button", { class: "btn sm " + (view === id ? "primary" : "ghost"), text: label, onclick: function () { view = id; draw(); } }); }
    function act(action, body, done) { api(action, {}, body).then(function (r) { toast(r.message || "Done.", r.changed === false || r.removed === false ? "bad" : "ok"); done(); }).catch(fail); }
    function drawAds() {
      var bar = h("div", { class: "owner-bar" }), list = h("div", null), detail = h("div", { class: "card" });
      [["pending", "Pending"], ["live", "Live"], ["paused", "Paused"], ["rejected", "Rejected"]].forEach(function (f) {
        bar.appendChild(h("button", { class: "btn sm " + (flt === f[0] ? "primary" : "ghost"), text: f[1], onclick: function () { flt = f[0]; draw(); } }));
      });
      out.appendChild(bar); out.appendChild(list); out.appendChild(detail);
      function show(id) {
        loadInto(detail, "owner_ad", { ad_id: String(id) }, function (j) {
          var a = j.ad;
          detail.appendChild(h("h3", { text: "#" + a.id + " " + (a.company_name || "") + ": " + (a.ad_title || "") }));
          detail.appendChild(h("p", { text: a.ad_description || "" }));
          detail.appendChild(kv([["Status", a.status], ["Link (text only, do not trust)", a.target_url], ["Budget (USD)", a.budget_usd], ["Submitter", a.user_id], ["Image", a.has_image ? "yes" : "no"], ["Submitted", a.submitted_at ? fmt(a.submitted_at) : "n/a"], ["Rejected because", a.rejection_reason || ""]]));
          function btn(label, cls, fn) { detail.appendChild(h("button", { class: "btn sm " + cls, text: label, onclick: fn })); }
          if (a.status === "pending") {
            btn("Approve", "", function () { act("owner_ad_approve", { ad_id: String(a.id) }, draw); });
            btn("Reject", "ghost", function () { var r = window.prompt("Reason (the submitter sees it, 200 characters max)"); if (r === null) return; act("owner_ad_reject", { ad_id: String(a.id), reason: r }, draw); });
          } else if (a.status === "approved") btn("Deactivate", "ghost", function () { act("owner_ad_deactivate", { ad_id: String(a.id) }, draw); });
          else if (a.status === "deactivated") btn("Reactivate", "", function () { act("owner_ad_reactivate", { ad_id: String(a.id) }, draw); });
        });
      }
      loadInto(list, "owner_ads", { status: flt }, function (j) {
        var c = j.counts || {};
        list.appendChild(h("p", { class: "muted", text: "Pending " + (c.pending || 0) + " | Live " + (c.approved || 0) + " | Paused " + (c.deactivated || 0) + " | Rejected " + (c.rejected || 0) + ". Newest first, up to " + j.limit + "." }));
        if (!j.rows.length) { list.appendChild(h("p", { class: "muted", text: "No ads here." })); return; }
        j.rows.forEach(function (r) { list.appendChild(h("button", { class: "btn sm ghost", text: "#" + r.id + " " + r.company_name + ": " + r.ad_title, onclick: function () { detail.textContent = ""; show(r.id); } })); });
      });
    }
    function drawMarket() {
      loadInto(out, "owner_market", { page: page }, function (j) {
        out.appendChild(h("p", { class: "muted", text: "Active listings, newest first. Removing needs a fresh sign-in and typed REMOVE, and there is no undo." }));
        out.appendChild(j.rows.length ? table(["Title", "Service", "Price", "Seller", "Clicks"], j.rows.map(function (r) { return [r.service_title, r.service_name, r.price_usd, r.user_id, r.clicks]; })) : h("p", { class: "muted", text: "No active listings on this page." }));
        j.rows.forEach(function (r) {
          out.appendChild(h("button", { class: "btn sm ghost", text: "Remove: " + (r.service_title || r.id), onclick: function () {
            var c = typed("REMOVE", "Remove listing \"" + (r.service_title || r.id) + "\" by seller " + r.user_id + "."); if (c === null) return;
            act("owner_listing_remove", { listing_id: r.id, confirm: c }, draw);
          } }));
        });
        out.appendChild(h("button", { class: "btn sm ghost", text: "Previous", disabled: page <= 0, onclick: function () { page--; draw(); } }));
        out.appendChild(h("button", { class: "btn sm ghost", text: "Next", disabled: !j.has_more, onclick: function () { page++; draw(); } }));
      });
    }
    function draw() {
      nav.textContent = ""; out.textContent = "";
      nav.appendChild(tab("ads", "Ads")); nav.appendChild(tab("market", "Marketplace"));
      return view === "ads" ? drawAds() : drawMarket();
    }
    shell("ads", h("div", null, nav, out));
    draw();
  }

  function bump() {
    var out = h("div", { class: "card" }); shell("bump", out);
    function draw() {
      out.textContent = "";
      loadInto(out, "owner_bump", {}, function (j) {
        out.appendChild(kv([["Cooldown", j.cooldown_seconds == null ? "default" : Math.round(j.cooldown_seconds / 60) + " min"], ["Servers set up (main bot)", j.guild_total], ["Listings waiting for review", j.pending_total]]));
        var m = h("input", { type: "text", placeholder: "New cooldown (minutes)", inputmode: "numeric", "aria-label": "Cooldown in minutes" });
        out.appendChild(h("div", { class: "owner-bar" }, m, h("button", { class: "btn sm", text: "Set cooldown", onclick: function () {
          api("owner_bump_cooldown", {}, { minutes: m.value.trim() }).then(function (r) { toast(r.message, "ok"); draw(); }).catch(fail);
        } })));
        out.appendChild(h("h3", { text: "Servers" }));
        out.appendChild(j.guilds.length ? table(["Server", "Channel", "Receives bumps", "Premium"], j.guilds.map(function (g) { return [g.guild_id, g.bump_channel_id, g.receives_bumps ? "yes" : "no", g.is_premium ? "yes" : "no"]; })) : h("p", { class: "muted", text: "No servers have set up bump." }));
        out.appendChild(h("h3", { text: "Waiting for review" }));
        out.appendChild(j.pending.length ? table(["Id", "Name", "Server", "Submitted by", "Description"], j.pending.map(function (p) { return [p.id, p.name, p.guild_id, p.verified_owner_id, p.description]; })) : h("p", { class: "muted", text: "Nothing waiting." }));
        out.appendChild(h("p", { class: "muted", text: "Approving or rejecting a bot listing is done in Discord (/admin bump review): it needs the live bot." }));
      });
    }
    draw();
  }

  /* ---------- Phase 4: safety ---------- */
  function watchlist() {
    var kind = "user", days = 7, out = h("div", { class: "card" }), bar = h("div", { class: "owner-bar" });
    function draw() {
      bar.textContent = ""; out.textContent = "";
      [["user", "Users"], ["guild", "Servers"]].forEach(function (k) { bar.appendChild(h("button", { class: "btn sm " + (kind === k[0] ? "primary" : "ghost"), text: k[1], onclick: function () { kind = k[0]; draw(); } })); });
      [7, 30].forEach(function (d) { bar.appendChild(h("button", { class: "btn sm " + (days === d ? "primary" : "ghost"), text: d + " days", onclick: function () { days = d; draw(); } })); });
      loadInto(out, "owner_watchlist", { kind: kind, days: days }, function (j) {
        out.appendChild(h("p", { class: "muted", text: j.note }));
        out.appendChild(j.rows.length ? table([kind === "user" ? "User" : "Server", "Auto-mod hits", "Servers", "Last"], j.rows.map(function (r) { return [r.target_id, r.hits, r.guilds, r.last_at ? fmt(r.last_at) : ""]; })) : h("p", { class: "muted", text: "Nobody on the list for this window." }));
      });
    }
    shell("watchlist", h("div", null, bar, out));
    draw();
  }

  function reports() {
    var out = h("div", { class: "card" }); shell("reports", out);
    function draw() {
      out.textContent = "";
      loadInto(out, "owner_reports", {}, function (j) {
        var c = j.counts || {};
        out.appendChild(h("p", { class: "muted", text: "New " + (c["new"] || 0) + " | Reviewed " + (c.reviewed || 0) + " | Dismissed " + (c.dismissed || 0) + ". Showing the newest " + j.limit + " new reports. Report text is written by other users: read it, do not trust it." }));
        if (!j.rows.length) { out.appendChild(h("p", { class: "muted", text: "No new reports." })); return; }
        j.rows.forEach(function (r) {
          out.appendChild(h("div", { class: "owner-bar" }, h("strong", { text: "#" + r.id + " server " + r.guild_id }), h("span", { class: "muted", text: fmt(r.created_at) }),
            h("button", { class: "btn sm", text: "Reviewed", onclick: function () { api("owner_report_resolve", {}, { report_id: String(r.id), status: "reviewed" }).then(function () { draw(); }).catch(fail); } }),
            h("button", { class: "btn sm ghost", text: "Dismiss", onclick: function () { api("owner_report_resolve", {}, { report_id: String(r.id), status: "dismissed" }).then(function () { draw(); }).catch(fail); } })));
          out.appendChild(h("p", { text: r.reason || "(no reason given)" }));
        });
      });
    }
    draw();
  }

  function statusPage() {
    var out = h("div", { class: "card" }); shell("status", out);
    function draw() {
      out.textContent = "";
      loadInto(out, "owner_status", {}, function (j) {
        out.appendChild(h("p", { class: "muted", text: j.note }));
        out.appendChild(h("p", { text: "Presence: " + (j.presences[j.presence] || j.presence) }));
        Object.keys(j.presences).forEach(function (k) { out.appendChild(h("button", { class: "btn sm " + (j.presence === k ? "primary" : "ghost"), text: j.presences[k], onclick: function () { api("owner_presence_set", {}, { presence: k }).then(function () { draw(); }).catch(fail); } })); });
        out.appendChild(h("h3", { text: "Custom statuses (" + j.entries.length + "/" + j.max_entries + ")" }));
        out.appendChild(j.entries.length ? table(["Id", "Type", "Text"], j.entries.map(function (e) { return [e.id, j.kinds[e.kind] || e.kind, e.text]; })) : h("p", { class: "muted", text: "None: the bot uses its built-in rotation." }));
        j.entries.forEach(function (e) { out.appendChild(h("button", { class: "btn sm ghost", text: "Remove " + e.id, onclick: function () { api("owner_status_remove", {}, { id: String(e.id) }).then(function () { draw(); }).catch(fail); } })); });
        var kind = h("select", { "aria-label": "Status type" }, Object.keys(j.kinds).map(function (k) { return h("option", { value: k, text: j.kinds[k] }); }));
        var text = h("input", { type: "text", maxlength: String(j.max_text), placeholder: "e.g. over {servers} servers", "aria-label": "Status text" });
        out.appendChild(h("div", { class: "owner-bar" }, kind, text, h("button", { class: "btn sm", text: "Add", onclick: function () {
          api("owner_status_add", {}, { kind: kind.value, text: text.value }).then(function (r) { toast(r.message, r.added ? "ok" : "bad"); draw(); }).catch(fail);
        } })));
        out.appendChild(h("button", { class: "btn sm ghost", text: "Reset to built-in rotation", onclick: function () {
          var c = typed("RESET", "Deletes every custom status and the presence override."); if (c === null) return;
          api("owner_status_reset", {}, { confirm: c }).then(function () { draw(); }).catch(fail);
        } }));
      });
    }
    draw();
  }

  function honeypot() {
    var out = h("div", { class: "card" }); shell("honeypot", out);
    loadInto(out, "owner_honeypot", {}, function (j) {
      out.appendChild(h("p", { class: "muted", text: j.note }));
      out.appendChild(kv([["Servers configured", j.totals.configured], ["Enabled", j.totals.enabled], ["Total triggers", j.totals.triggers]]));
      out.appendChild(j.rows.length ? table(["Server", "Bot", "Enabled", "Action", "Channel", "Triggers", "Last triggered"], j.rows.map(function (r) { return [r.guild_id, r.clone_id ? "clone " + r.clone_id : "main", r.enabled ? "yes" : "no", r.action, r.channel_id, r.triggered_count, r.last_triggered_at ? fmt(r.last_triggered_at) : "never"]; })) : h("p", { class: "muted", text: "No honeypots set up." }));
    });
  }

  function scamshield() {
    var out = h("div", { class: "card" }); shell("scamshield", out);
    function draw() {
      out.textContent = "";
      loadInto(out, "owner_scamshield", {}, function (j) {
        out.appendChild(h("p", { class: "muted", text: j.note + " Staff (Manage Messages / Manage Server / Admin) are never checked. Scam images can only be added in Discord." }));
        out.appendChild(h("p", { text: j.enabled ? "ON in every server." : "OFF. Nothing is being checked." }));
        out.appendChild(h("button", { class: "btn sm " + (j.enabled ? "ghost" : "primary"), text: j.enabled ? "Turn OFF" : "Turn ON", onclick: function () {
          var b = { enabled: !j.enabled };
          if (j.enabled) { var c = typed("DISABLE", "This stops scam filtering in EVERY server."); if (c === null) return; b.confirm = c; }
          api("owner_scam_toggle", {}, b).then(function (r) { toast(r.message, "ok"); draw(); }).catch(fail);
        } }));
        out.appendChild(kv([["Words", j.counts.word], ["Domains", j.counts.domain], ["Known scam images", j.counts.image], ["Caught so far", j.hit_total], ["Rule limit", j.max_rules]]));
        var t = h("input", { type: "text", maxlength: "100", placeholder: "Word, phrase or domain", "aria-label": "New rule" });
        out.appendChild(h("div", { class: "owner-bar" }, t, h("button", { class: "btn sm", text: "Add rule", onclick: function () {
          api("owner_scam_add", {}, { text: t.value }).then(function (r) { toast(r.message, r.added ? "ok" : "bad"); draw(); }).catch(fail);
        } })));
        out.appendChild(h("h3", { text: "Rules" }));
        out.appendChild(j.rules.length ? table(["#", "Kind", "Pattern", "Note"], j.rules.map(function (r) { return [r.id, r.kind, r.kind === "image" ? "image " + r.pattern : r.pattern, r.note || ""]; })) : h("p", { class: "muted", text: "No rules yet." }));
        j.rules.forEach(function (r) { out.appendChild(h("button", { class: "btn sm ghost", text: "Remove #" + r.id, onclick: function () { api("owner_scam_remove", {}, { id: String(r.id) }).then(function (x) { toast(x.message, x.removed ? "ok" : "bad"); draw(); }).catch(fail); } })); });
        out.appendChild(h("h3", { text: "Latest catches" }));
        out.appendChild(j.hits.length ? table(["When", "Server", "User", "Kind", "Deleted"], j.hits.map(function (x) { return [fmt(x.created_at), x.guild_id, x.user_id, x.kind, x.deleted ? "yes" : "NO"]; })) : h("p", { class: "muted", text: "None yet." }));
      });
    }
    draw();
  }

  /* ---------- Phase 5: helpers, clones, database (owner-only, step-up on the dangerous ones) ---------- */
  function helpersPage() {
    var out = h("div", { class: "card" }); shell("helpers", out);
    function draw() {
      out.textContent = "";
      loadInto(out, "owner_helpers", {}, function (j) {
        out.appendChild(h("p", { class: "muted", text: j.note }));
        var uid = h("input", { type: "text", inputmode: "numeric", placeholder: "User id", "aria-label": "Helper user id" });
        var boxes = Object.keys(j.grantable).map(function (k) {
          var cb = h("input", { type: "checkbox", value: k, id: "hp-" + k });
          return { k: k, cb: cb, el: h("label", null, cb, " " + j.grantable[k]) };
        });
        out.appendChild(h("div", { class: "owner-bar" }, uid, boxes.map(function (b) { return b.el; }), h("button", { class: "btn sm", text: "Save helper", onclick: function () {
          var sections = boxes.filter(function (b) { return b.cb.checked; }).map(function (b) { return b.k; });
          api("owner_helper_set", {}, { user_id: uid.value.trim(), sections: sections }).then(function (r) { toast(r.message, "ok"); draw(); }).catch(fail);
        } })));
        out.appendChild(h("h3", { text: "Current helpers" }));
        out.appendChild(j.rows.length ? table(["User", "Sections", "Added by", "Updated"], j.rows.map(function (r) { return [r.user_id, r.sections.join(", "), r.added_by, fmt(r.updated_at || r.created_at)]; })) : h("p", { class: "muted", text: "No helpers." }));
        j.rows.forEach(function (r) { out.appendChild(h("button", { class: "btn sm ghost", text: "Remove " + r.user_id, onclick: function () {
          var c = typed("REMOVE", "Remove helper " + r.user_id + "."); if (c === null) return;
          api("owner_helper_remove", {}, { user_id: r.user_id, confirm: c }).then(function (x) { toast(x.message, x.removed ? "ok" : "bad"); draw(); }).catch(fail);
        } })); });
      });
    }
    draw();
  }

  function clonesPage() {
    var out = h("div", { class: "card" }); shell("clones", out);
    function tokenInput(label) { return h("input", { type: "password", autocomplete: "off", spellcheck: "false", placeholder: label, "aria-label": label }); }
    function done(r, what) { toast(what + " " + (r.bot_username || "") + ". " + r.note, "ok"); }
    function draw() {
      out.textContent = "";
      loadInto(out, "owner_clones", {}, function (j) {
        out.appendChild(h("p", { class: "muted", text: "Tokens are sent once, stored encrypted, and never shown again. Each action needs a fresh sign-in (Security page). " + j.stop_note }));
        var tok = tokenInput("Bot token for a new clone");
        out.appendChild(h("div", { class: "owner-bar" }, tok, h("button", { class: "btn sm", text: "Register my clone", onclick: function () {
          var c = typed("REGISTER", "Register a new clone for your account."); if (c === null) return;
          var t = tok.value; tok.value = "";
          api("owner_clone_register", {}, { token: t, confirm: c }).then(function (r) { done(r, "Registered"); draw(); }).catch(fail);
        } })));
        var rid = h("input", { type: "text", inputmode: "numeric", placeholder: "Clone number", "aria-label": "Clone number to relink" }), rtok = tokenInput("New bot token");
        out.appendChild(h("div", { class: "owner-bar" }, rid, rtok, h("button", { class: "btn sm ghost", text: "Relink my clone", onclick: function () {
          var c = typed("RELINK", "Point clone " + rid.value + " at a new bot token. Its data stays."); if (c === null) return;
          var t = rtok.value; rtok.value = "";
          api("owner_clone_relink", {}, { clone: rid.value.trim(), token: t, confirm: c }).then(function (r) { done(r, "Relinked to"); draw(); }).catch(fail);
        } })));
        out.appendChild(h("h3", { text: "All clones" }));
        out.appendChild(j.rows.length ? table(["#", "Bot", "Owner", "Status", "Last heartbeat", "Created"], j.rows.map(function (r) {
          return [r.clone_id, r.bot_username, r.owner_id, r.status, r.last_heartbeat ? fmt(r.last_heartbeat) : "never", fmt(r.created_at)]; })) : h("p", { class: "muted", text: "No clones." }));
        j.rows.filter(function (r) { return r.status === "active"; }).forEach(function (r) { out.appendChild(h("button", { class: "btn sm ghost", text: "Stop #" + r.clone_id, onclick: function () {
          var c = typed("STOP", "Stop clone #" + r.clone_id + " (" + (r.bot_username || "unnamed") + "). Its servers lose the bot until it is relinked."); if (c === null) return;
          api("owner_clone_stop", {}, { clone: String(r.clone_id), confirm: c }).then(function (x) { toast(x.message, "ok"); draw(); }).catch(fail);
        } })); });
      });
    }
    draw();
  }

  function databasePage() {
    var out = h("div", { class: "card" }); shell("database", out);
    function draw() {
      out.textContent = "";
      loadInto(out, "owner_database", {}, function (j) {
        out.appendChild(h("p", { class: "muted", text: "Read-only counts plus one named cleanup. There is no way to run your own SQL here." }));
        out.appendChild(table(["Table", "Rows"], j.counts.map(function (c) { return [c.table, c.rows == null ? "n/a" : (c.approx ? "~" : "") + c.rows]; })));
        out.appendChild(h("p", { text: j.stale_payments + " checkout(s) pending for more than " + j.stale_hours + " hours." }));
        out.appendChild(h("button", { class: "btn sm", text: "Mark stale checkouts expired", disabled: !j.stale_payments, onclick: function () {
          var c = typed("CLEANUP", "Mark " + j.stale_payments + " abandoned checkout(s) as expired. Rows are kept."); if (c === null) return;
          api("owner_db_cleanup_stale", {}, { confirm: c }).then(function (r) { toast(r.message, "ok"); draw(); }).catch(fail);
        } }));
      });
    }
    draw();
  }

  window.DashOwner = {
    render: function (sub) {
      var page = PAGES.filter(function (p) { return p.id === (sub || "") && allowed(p); })[0];
      if (!page) { location.hash = "#/owner"; return; }
      ({ "": overview, health: health, servers: servers, users: users, payments: payments, audit: audit, security: security,
        controls: controls, blacklist: blacklist, premium: premium, announce: announce, feedback: feedback, logs: logs, config: config,
        ads: ads, bump: bump, watchlist: watchlist, reports: reports, status: statusPage, honeypot: honeypot, scamshield: scamshield,
        helpers: helpersPage, clones: clonesPage, database: databasePage })[page.id]();
    }
  };
})();
