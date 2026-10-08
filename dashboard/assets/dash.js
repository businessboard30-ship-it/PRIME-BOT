/* PRIME BOT control panel. Plain JS, no build step. All server data is written with
   textContent / attributes (never innerHTML), so channel and server names can't inject markup. */
(function () {
  "use strict";
  var CFG = window.DASH_CONFIG, API = CFG.API_BASE.replace(/\/$/, "") + "/api/dash";
  var KEY = "primebot.dash.session";
  var NS = "http://www.w3.org/2000/svg";
  var app = document.getElementById("app"), hdrRight = document.getElementById("hdrRight"), menuBtn = document.getElementById("menuBtn");
  var S = { sid: null, user: null, servers: null, schema: null, guild: null, meta: null, orig: {}, draft: {}, mod: null, premium: false, errors: {}, unread: 0, isOwner: false, inbox: null };

  /* ---------- tiny DOM helpers ---------- */
  function h(tag, attrs) {
    var el = document.createElement(tag), i, k, v;
    if (attrs) for (k in attrs) {
      v = attrs[k]; if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  function add(el, c) {
    if (c == null || c === false) return;
    if (Array.isArray(c)) c.forEach(function (x) { add(el, x); });
    else el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  function svg(tag, attrs) {
    var el = document.createElementNS(NS, tag), k;
    for (k in attrs) el.setAttribute(k, attrs[k]);
    for (var i = 2; i < arguments.length; i++) if (arguments[i]) el.appendChild(arguments[i]);
    return el;
  }
  var ICONS = {
    home: "M4 11l8-7 8 7v9H4z M10 20v-6h4v6",
    wave: "M3 12c2-4 4-4 6 0s4 4 6 0 4-4 6 0",
    "shield-check": "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z M8.5 12l2.5 2.5 4.5-5",
    siren: "M6 18v-6a6 6 0 0 1 12 0v6 M4 18h16 M12 3v2 M3.5 6.5l1.5 1.5 M20.5 6.5L19 8",
    gavel: "M14 4l6 6-3 3-6-6z M11 7L4 14l3 3 7-7 M4 20h9",
    scroll: "M6 4h12v14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2z M9 8h6 M9 12h6 M9 16h3",
    bug: "M9 9a3 3 0 0 1 6 0v6a3 3 0 0 1-6 0z M4 10l3 1 M20 10l-3 1 M4 18l3-1 M20 18l-3-1 M9 5L7 3 M15 5l2-2",
    door: "M6 3h12v18H6z M14 12h2",
    trophy: "M8 4h8v5a4 4 0 0 1-8 0z M8 6H4v1a4 4 0 0 0 4 4 M16 6h4v1a4 4 0 0 1-4 4 M12 13v4 M8 20h8 M10 17h4",
    mic: "M12 3a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z M6 11a6 6 0 0 0 12 0 M12 17v4 M9 21h6",
    star: "M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z",
    ticket: "M4 7h16v3a2 2 0 0 0 0 4v3H4v-3a2 2 0 0 0 0-4z M14 7v10",
    bulb: "M9 18h6 M10 21h4 M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z",
    link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1 M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
    coins: "M12 4c4.4 0 8 1.3 8 3s-3.6 3-8 3-8-1.3-8-3 3.6-3 8-3z M4 7v5c0 1.7 3.6 3 8 3s8-1.3 8-3V7 M4 12v5c0 1.7 3.6 3 8 3s8-1.3 8-3v-5"
  };
  function icon(name) {
    return svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, svg("path", { d: ICONS[name] || ICONS.star }));
  }
  function toast(msg, kind) {
    var t = h("div", { class: "toast " + (kind || ""), text: msg });
    document.getElementById("toasts").appendChild(t);
    setTimeout(function () { t.remove(); }, kind === "bad" ? 6500 : 3200);
  }
  function initial(name) { return (name || "?").trim().charAt(0).toUpperCase() || "?"; }
  function avatar(url, name) {
    var a = h("div", { class: "ava" });
    if (url) a.appendChild(h("img", { src: url, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }));
    else a.textContent = initial(name);
    return a;
  }

  /* ---------- API ---------- */
  function api(action, params, body) {
    var url = API + "?action=" + encodeURIComponent(action), k;
    for (k in (params || {})) url += "&" + k + "=" + encodeURIComponent(params[k]);
    var opts = { headers: {}, cache: "no-store" };
    if (S.sid) opts.headers.Authorization = "Bearer " + S.sid;
    if (body) { opts.method = "POST"; opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(Object.assign({ action: action }, body)); }
    return fetch(url, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (r.status === 401) { signedOut("Your session expired. Sign in again."); throw new Error("401"); }
        if (!r.ok || j.ok === false) { var e = new Error(j.message || "Something went wrong."); e.status = r.status; e.payload = j; throw e; }
        return j;
      });
    }, function () { throw new Error("Can't reach the server. Check your connection."); });
  }
  function signedOut(msg) {
    localStorage.removeItem(KEY); S.sid = S.user = S.servers = null; S.guild = null; S.unread = 0; S.isOwner = false; S.inbox = null;
    renderHeader(); renderLogin(msg);
  }

  /* ---------- header ---------- */
  function renderHeader() {
    hdrRight.textContent = "";
    var inGuild = /^#\/g\//.test(location.hash);
    menuBtn.hidden = !(S.user && inGuild);
    if (!S.user) return;
    hdrRight.appendChild(h("a", { class: "btn sm ghost inboxbtn", href: "#/inbox", "aria-label": "Drop box, " + S.unread + " unread" }, "Drop box",
      S.unread ? h("span", { class: "badge-n", text: S.unread > 99 ? "99+" : String(S.unread) }) : null));
    hdrRight.appendChild(h("div", { class: "user" }, h("img", { src: S.user.avatar_url, alt: "" }), h("span", { text: S.user.username })));
    hdrRight.appendChild(h("button", { class: "btn sm ghost", text: "Sign out", onclick: function () {
      api("logout", {}, {}).catch(function () {}).then(function () { signedOut(); });
    } }));
  }
  menuBtn.addEventListener("click", function () { document.body.classList.toggle("menu"); });

  /* ---------- login ---------- */
  function ringsArt() {
    var c = function (cls, r, w, dash, s) { return svg("circle", { "class": cls, cx: 180, cy: 180, r: r, fill: "none", stroke: "#f2faff", "stroke-width": w, "stroke-dasharray": dash, style: "--s:" + s }); };
    return svg("svg", { "class": "rings", viewBox: "0 0 360 360", "aria-hidden": "true" },
      svg("polygon", { points: "345,180 262.5,322.9 97.5,322.9 15,180 97.5,37.1 262.5,37.1", fill: "none", stroke: "#f2faff", "stroke-width": 2.2 }),
      c("spin", 118, 8, "110 18 55 22 190 24 90 30", "26s"), c("spin rev", 96, 4, "40 8 12 8 200 10", "16s"),
      c("spin", 76, 11, "190 66 110 94", "10s"), c("spin rev", 142, 6, "1.6 8", "34s"),
      svg("circle", { "class": "pulse", cx: 180, cy: 180, r: 14, fill: "#f2faff" }));
  }
  function renderLogin(msg) {
    document.body.classList.remove("menu");
    app.textContent = "";
    var err = h("p", { class: "err", role: "alert", text: msg || "" });
    app.appendChild(h("main", { id: "main", class: "center" }, h("div", { class: "login" },
      h("div", { class: "rv", style: "--i:0" }, ringsArt()),
      h("p", { class: "eyebrow rv", style: "--i:1", text: "Server control panel" }),
      h("h1", { class: "rv", style: "--i:2", text: "Run your server from one place." }),
      h("p", { class: "lead rv", style: "--i:3", text: "Sign in with Discord to configure welcome cards, verification, anti-raid, moderation, leveling and more. You only see servers where you can manage the server." }),
      h("a", { class: "btn primary rv", style: "--i:4", href: API + "?action=login", text: "Sign in with Discord" }),
      err,
      h("ul", { class: "rv", style: "--i:5" }, h("li", { text: "Live settings" }), h("li", { text: "No commands needed" }), h("li", { text: "Per-server access" })))));
    renderHeader();
  }

  /* ---------- servers ---------- */
  function renderServers() {
    document.body.classList.remove("menu"); renderHeader();
    var grid = h("div", { class: "grid" }, [1, 2, 3].map(function () { return h("div", { class: "skel" }); }));
    app.textContent = "";
    app.appendChild(h("main", { id: "main", class: "page" },
      h("div", { class: "page-head rv" }, h("p", { class: "eyebrow", text: "Welcome back, " + S.user.username }), h("h1", { text: "Choose a server" }),
        h("p", { class: "muted", text: "Servers where you have Manage Server. Pick one that has the bot to configure it." })), grid));
    var done = function (servers) {
      grid.textContent = "";
      if (!servers.length) {
        grid.appendChild(h("div", { class: "card empty", style: "grid-column:1/-1" }, h("h3", { text: "No servers yet" }),
          h("p", { class: "muted", text: "You need Manage Server in a Discord server to see it here." })));
        return;
      }
      servers.forEach(function (g, i) {
        var inner = [h("div", { class: "srv-top" }, avatar(g.icon_url, g.name), h("div", null, h("h3", { text: g.name }),
          h("span", { class: "tag" }, h("i", { class: "dot " + (g.bot_present ? "on live" : "off") }), g.bot_present ? "Bot online" : "Bot not added"))),
          h("span", { class: "btn sm " + (g.bot_present ? "primary" : "ghost"), text: g.bot_present ? "Manage" : "Add the bot" })];
        var el = g.bot_present
          ? h("a", { class: "card srv rv", style: "--i:" + i, href: "#/g/" + g.id }, inner)
          : h("a", { class: "card srv rv", style: "--i:" + i, href: CFG.INVITE_URL + "&guild_id=" + g.id + "&disable_guild_select=true", target: "_blank", rel: "noopener" }, inner);
        grid.appendChild(el);
      });
    };
    if (S.servers) return done(S.servers);
    api("me").then(function (j) { S.servers = j.servers; S.user = j.user; S.unread = j.unread || 0; S.isOwner = !!j.is_owner; renderHeader(); done(S.servers); })
      .catch(function (e) { if (e.message !== "401") { grid.textContent = ""; grid.appendChild(h("div", { class: "card empty", style: "grid-column:1/-1" }, h("p", { text: e.message }), h("button", { class: "btn sm", text: "Try again", onclick: renderServers }))); } });
  }

  /* ---------- guild shell ---------- */
  function mods() { return S.schema.modules; }
  function modById(id) { return mods().filter(function (m) { return m.id === id; })[0]; }
  function statusOf(id) { var s = S.guild && S.guild.status ? S.guild.status[id] : null; return s === true ? "on" : s === false ? "off" : ""; }

  function loadGuild(gid) {
    if (S.guild && S.guild.id === gid && S.meta) return Promise.resolve();
    return Promise.all([api("guild", { guild_id: gid }), api("meta", { guild_id: gid })]).then(function (r) {
      S.guild = r[0].guild; S.meta = r[1]; S.premium = !!S.guild.premium;
    });
  }
  function renderShell(gid, modId) {
    app.textContent = ""; renderHeader();
    var nav = h("nav", { class: "nav", "aria-label": "Settings" });
    var search = h("input", { type: "text", class: "search", placeholder: "Search settings", "aria-label": "Search settings", autocomplete: "off" });
    var side = h("aside", { class: "side", id: "side" },
      h("div", { class: "srvhead" }, avatar(S.guild.icon_url, S.guild.name), h("div", null, h("b", { text: S.guild.name }), h("a", { href: "#/", text: "Switch server" }))),
      search, nav);
    function link(href, name, label, current, mod) {
      return h("a", { href: href, "aria-current": current ? "page" : null, "data-q": (label + " " + ((mod && mod.desc) || "")).toLowerCase() },
        icon(name), h("span", { text: label }), mod && statusOf(mod.id) ? h("i", { class: "dot " + statusOf(mod.id), title: statusOf(mod.id) === "on" ? "Enabled" : "Disabled" }) : null);
    }
    nav.appendChild(link("#/g/" + gid, "home", "Overview", !modId));
    nav.appendChild(link("#/g/" + gid + "/audit", "scroll", "Audit log", modId === "audit"));
    S.schema.categories.forEach(function (cat) {
      var list = mods().filter(function (m) { return m.category === cat; });
      if (!list.length) return;
      nav.appendChild(h("div", { class: "grp", text: cat }));
      list.forEach(function (m) { nav.appendChild(link("#/g/" + gid + "/" + m.id, m.icon, m.title, m.id === modId, m)); });
    });
    search.addEventListener("input", function () {
      var q = search.value.trim().toLowerCase();
      nav.querySelectorAll("a").forEach(function (a) { a.style.display = !q || a.getAttribute("data-q").indexOf(q) > -1 ? "" : "none"; });
      nav.querySelectorAll(".grp").forEach(function (g) {
        var n = g.nextElementSibling, any = false;
        while (n && !n.classList.contains("grp")) { if (n.style.display !== "none") any = true; n = n.nextElementSibling; }
        g.style.display = any ? "" : "none";
      });
    });
    var main = h("main", { id: "main", class: "main" });
    app.appendChild(h("div", { class: "shell" }, side, main));
    side.addEventListener("click", function (e) { if (e.target.closest("a")) document.body.classList.remove("menu"); });
    return main;
  }

  function renderOverview(gid, main) {
    var g = S.guild, on = 0, total = 0;
    mods().forEach(function (m) { var s = statusOf(m.id); if (s) { total++; if (s === "on") on++; } });
    function stat(v, l) { return h("div", { class: "card stat rv" }, h("b", { text: String(v) }), h("span", { text: l })); }
    var quick = h("div", { class: "quick" });
    mods().forEach(function (m, i) {
      var isToggle = m.fields.length && m.fields[0].key === "enabled" && !m.no_quick;
      var row = h("div", { class: "card q rv", style: "--i:" + Math.min(i, 12) }, icon(m.icon),
        h("div", { class: "grow" }, h("a", { href: "#/g/" + gid + "/" + m.id, text: m.title }), h("small", { text: m.desc })));
      if (isToggle) {
        var sw = h("button", { class: "switch", role: "switch", "aria-label": m.title, "aria-checked": String(statusOf(m.id) === "on") });
        sw.addEventListener("click", function () {
          var next = sw.getAttribute("aria-checked") !== "true"; sw.disabled = true;
          api("save", {}, { guild_id: gid, module: m.id, values: { enabled: next } }).then(function (r) {
            S.guild.status[m.id] = !!r.values.enabled; sw.setAttribute("aria-checked", String(!!r.values.enabled));
            toast(m.title + (r.values.enabled ? " is on" : " is off"), "ok");
            var d = document.querySelector('.nav a[href="#/g/' + gid + "/" + m.id + '"] .dot'); if (d) d.className = "dot " + (r.values.enabled ? "on" : "off");
          }).catch(function (e) { toast(e.message, "bad"); }).then(function () { sw.disabled = false; });
        });
        row.appendChild(sw);
      } else row.appendChild(h("a", { class: "btn sm ghost", href: "#/g/" + gid + "/" + m.id, text: "Open" }));
      quick.appendChild(row);
    });
    add(main, [
      h("div", { class: "page-head rv" }, h("p", { class: "crumb", text: "Overview" }), h("h1", { text: g.name })),
      h("div", { class: "stats" }, stat(g.members == null ? "–" : g.members.toLocaleString(), "Members"), stat(g.online == null ? "–" : g.online.toLocaleString(), "Online now"),
        stat(on + " / " + total, "Features on"), stat(g.premium ? "Active" : "Free", "Plan")),
      !g.premium ? h("div", { class: "notice rv" }, "Premium unlocks anti-raid filters, honeypot extras and more. ", h("a", { href: CFG.SITE_URL + "/pricing/", target: "_blank", rel: "noopener", text: "See plans" })) : null,
      h("h2", { text: "Quick controls" }), quick]);
  }

  /* ---------- module form ---------- */
  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
  function dirtyKeys() { return Object.keys(S.draft).filter(function (k) { return !same(S.draft[k], S.orig[k]); }); }

  function control(f, onChange) {
    var v = S.draft[f.key], locked = !S.premium && !!f.premium, el;
    function set(x) { S.draft[f.key] = x; onChange(); }
    if (f.type === "toggle") {
      el = h("button", { class: "switch", role: "switch", id: "f_" + f.key, "aria-checked": String(!!v), disabled: locked });
      el.addEventListener("click", function () { var n = el.getAttribute("aria-checked") !== "true"; el.setAttribute("aria-checked", String(n)); set(n); });
    } else if (f.type === "number") {
      el = h("input", { type: "number", id: "f_" + f.key, min: f.min, max: f.max, value: v == null ? "" : v, disabled: locked });
      el.addEventListener("input", function () { set(el.value === "" ? null : Number(el.value)); });
    } else if (f.type === "select") {
      el = h("select", { id: "f_" + f.key, disabled: locked });
      f.options.forEach(function (o) {
        var gated = !S.premium && f.premium_values && f.premium_values.indexOf(o[0]) > -1;
        var op = h("option", { value: String(o[0]), text: o[1], disabled: gated });
        if (String(o[0]) === String(v)) op.selected = true; el.appendChild(op);
      });
      el.addEventListener("change", function () { set(f.numeric ? Number(el.value) : el.value); });
    } else if (f.type === "text") {
      el = h("input", { type: "text", id: "f_" + f.key, value: v || "", maxlength: f.maxlen || 200, disabled: locked, autocomplete: "off" });
      el.addEventListener("input", function () { set(el.value); });
    } else if (f.type === "textarea" || f.type === "list") {
      var isList = f.type === "list", max = f.maxlen || 1000;
      var ta = h("textarea", { id: "f_" + f.key, disabled: locked, maxlength: isList ? null : max, spellcheck: "false" });
      ta.value = isList ? (v || []).join("\n") : (v || "");
      var cnt = h("div", { class: "count" });
      var upd = function () { cnt.textContent = isList ? ta.value.split("\n").filter(Boolean).length + " entries" : ta.value.length + " / " + max; };
      ta.addEventListener("input", function () { upd(); set(isList ? ta.value.split("\n").map(function (s) { return s.trim(); }).filter(Boolean) : ta.value); });
      upd(); el = h("div", null, ta, cnt);
    } else if (f.type === "color") {
      var pick = h("input", { type: "color", "aria-label": f.label + " picker", value: /^#[0-9a-f]{6}$/i.test(v || "") ? v : "#5865f2", disabled: locked });
      var hex = h("input", { type: "text", id: "f_" + f.key, value: v || "", maxlength: 7, placeholder: "#5865F2", disabled: locked, autocomplete: "off" });
      pick.addEventListener("input", function () { hex.value = pick.value; set(pick.value); });
      hex.addEventListener("input", function () { if (/^#[0-9a-f]{6}$/i.test(hex.value)) pick.value = hex.value; set(hex.value); });
      el = h("div", { class: "colorrow" }, pick, hex);
    } else if (f.type === "channel" || f.type === "role") {
      var items = f.type === "role" ? S.meta.roles : (S.meta.channels[f.kind || "text"] || []);
      el = h("select", { id: "f_" + f.key, disabled: locked }, h("option", { value: "", text: "Not set" }));
      var seen = false;
      items.forEach(function (c) {
        var pre = f.type === "role" ? "@ " : f.kind === "voice" ? "~ " : f.kind === "category" ? "" : "# ";
        var op = h("option", { value: c.id, text: pre + c.name });
        if (c.id === v) { op.selected = true; seen = true; } el.appendChild(op);
      });
      if (v && !seen) { var gone = h("option", { value: v, text: "(deleted " + f.type + ")" }); gone.selected = true; el.appendChild(gone); }
      el.addEventListener("change", function () { set(el.value || null); });
    }
    return el;
  }

  function renderModule(gid, m, main) {
    S.mod = m.id;
    var bar = h("div", { class: "savebar", role: "region", "aria-label": "Unsaved changes" });
    var msg = h("p", { text: "Unsaved changes" });
    var saveBtn = h("button", { class: "btn sm primary", text: "Save changes" });
    var resetBtn = h("button", { class: "btn sm ghost", text: "Discard" });
    bar.appendChild(msg); bar.appendChild(resetBtn); bar.appendChild(saveBtn);
    var form = h("div", { class: "card fields rv" });
    var designer = m.designer === "welcome" ? makeWelcomePreview(gid) : null;

    function refresh() {
      if (designer) designer.update();
      var keys = dirtyKeys();
      form.querySelectorAll(".field").forEach(function (row) { row.classList.toggle("chg", keys.indexOf(row.getAttribute("data-key")) > -1); });
      msg.textContent = keys.length + (keys.length === 1 ? " unsaved change" : " unsaved changes");
      bar.classList.toggle("show", keys.length > 0);
    }
    function build() {
      form.textContent = "";
      m.fields.forEach(function (f) {
        var locked = !S.premium && !!f.premium;
        var row = h("div", { class: "field", "data-key": f.key },
          h("div", null, h("label", { for: "f_" + f.key }, f.label, locked ? h("span", { class: "badge", text: "Premium" }) : null),
            f.help ? h("p", { class: "help", text: f.help }) : null,
            S.errors[f.key] ? h("p", { class: "msg", role: "alert", text: S.errors[f.key] }) : null),
          control(f, refresh));
        form.appendChild(row);
      });
      refresh();
    }
    resetBtn.addEventListener("click", function () { S.draft = JSON.parse(JSON.stringify(S.orig)); S.errors = {}; build(); });
    saveBtn.addEventListener("click", function () {
      var keys = dirtyKeys(), vals = {};
      if (!keys.length) return;
      keys.forEach(function (k) { vals[k] = S.draft[k]; });
      /* The server flips a welcome card to the plain style when a colour is saved without a card type.
         Send the card type explicitly so what you saved is what the preview showed. */
      if (m.id === "welcome" && (("accent_color" in vals) || ("background_color" in vals)) && !("use_template" in vals)) vals.use_template = !!S.draft.use_template;
      saveBtn.classList.add("busy"); S.errors = {};
      api("save", {}, { guild_id: gid, module: m.id, values: vals }).then(function (r) {
        S.orig = r.values; S.draft = JSON.parse(JSON.stringify(r.values));
        if ("enabled" in r.values) { S.guild.status[m.id] = !!r.values.enabled; var d = document.querySelector('.nav a[aria-current="page"] .dot'); if (d) d.className = "dot " + (r.values.enabled ? "on" : "off"); }
        build(); toast("Saved " + m.title, "ok");
      }).catch(function (e) {
        var errs = (e.payload && e.payload.errors) || [e.message];
        toast(errs[0], "bad");
        errs.forEach(function (t) { m.fields.forEach(function (f) { if (t.indexOf(f.label + ":") === 0) S.errors[f.key] = t.slice(f.label.length + 1).trim(); }); });
        build();
      }).then(function () { saveBtn.classList.remove("busy"); });
    });

    add(main, [
      h("div", { class: "panel-head rv" }, h("div", null, h("p", { class: "crumb", text: m.category }), h("h1", { text: m.title }), h("p", { class: "muted", text: m.desc })),
        h("a", { class: "btn sm ghost", href: "#/g/" + gid, text: "Overview" })),
      m.note ? h("div", { class: "notice rv" }, m.note) : null,
      !S.premium && m.fields.some(function (f) { return f.premium || f.premium_values; })
        ? h("div", { class: "notice rv" }, "Some options here are Premium. ", h("a", { href: CFG.SITE_URL + "/pricing/", target: "_blank", rel: "noopener", text: "See plans" })) : null,
      designer ? designer.el : null, form, bar]);
    form.appendChild(h("div", { class: "skel", style: "height:160px" }));
    api("config", { guild_id: gid, module: m.id }).then(function (r) {
      S.premium = !!r.premium; S.orig = r.values; S.draft = JSON.parse(JSON.stringify(r.values)); S.errors = {}; build();
    }).catch(function (e) { if (e.message !== "401") { form.textContent = ""; form.appendChild(h("p", { text: e.message })); } });
  }

  /* ---------- drop box + announcements ---------- */
  var KIND_LABEL = { info: "Info", update: "Update", warning: "Warning", maintenance: "Maintenance" };
  function when(iso) { try { return new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }); } catch (e) { return ""; } }
  function setUnread(n) { S.unread = Math.max(0, n); renderHeader(); }

  function showAnnouncements() {
    var box = document.getElementById("announce");
    if (!box) { box = h("div", { id: "announce", class: "announce", role: "region", "aria-label": "Announcements" }); document.getElementById("hdr").after(box); }
    box.textContent = "";
    if (!S.sid) return;
    api("dropbox").then(function (j) {
      S.inbox = j.messages; setUnread(j.unread);
      box.textContent = "";
      j.messages.filter(function (m) { return m.announce && !m.read; }).slice(0, 3).forEach(function (m) {
        var row = h("div", { class: "ann " + m.kind },
          h("div", { class: "grow" }, h("b", { text: m.title }), h("span", { text: " " + m.body })),
          h("button", { class: "btn sm ghost", text: "Dismiss", onclick: function () {
            api("dropbox_read", {}, { id: m.id }).then(function () { row.remove(); setUnread(S.unread - 1); }).catch(function (e) { toast(e.message, "bad"); });
          } }));
        box.appendChild(row);
      });
    }).catch(function () {});
  }

  function renderComposer(onSent) {
    var title = h("input", { type: "text", maxlength: 100, placeholder: "Title", "aria-label": "Title", autocomplete: "off" });
    var body = h("textarea", { maxlength: 2000, placeholder: "Message to every dashboard admin", "aria-label": "Message", spellcheck: "true" });
    var kind = h("select", { "aria-label": "Type" }, Object.keys(KIND_LABEL).map(function (k) { return h("option", { value: k, text: KIND_LABEL[k] }); }));
    var expiry = h("select", { "aria-label": "Expires" }, [["", "Never expires"], ["24", "In 1 day"], ["168", "In 7 days"], ["720", "In 30 days"]].map(function (o) { return h("option", { value: o[0], text: o[1] }); }));
    var ann = h("input", { type: "checkbox", id: "annchk" });
    var send = h("button", { class: "btn primary", text: "Send to all admins" });
    send.addEventListener("click", function () {
      if (!title.value.trim() || !body.value.trim()) return toast("Add a title and a message.", "bad");
      if (!confirm("Send this to every admin who uses the dashboard?")) return;
      send.classList.add("busy");
      api("dropbox_send", {}, { title: title.value, body: body.value, kind: kind.value, announce: ann.checked, expires_hours: expiry.value ? Number(expiry.value) : null })
        .then(function () { toast("Sent to the drop box", "ok"); title.value = body.value = ""; ann.checked = false; onSent(); })
        .catch(function (e) { toast(e.message, "bad"); }).then(function () { send.classList.remove("busy"); });
    });
    return h("div", { class: "card composer rv" }, h("h2", { text: "Send a message" }),
      h("p", { class: "muted", text: "Appears in every admin's drop box. Tick the banner option to also show it at the top of the panel until they dismiss it." }),
      title, body, h("div", { class: "row" }, kind, expiry, h("label", { class: "chk", for: "annchk" }, ann, " Also show as banner")), send);
  }

  function renderInbox() {
    document.body.classList.remove("menu"); S.mod = null; renderHeader();
    var list = h("div", { class: "inbox" }, h("div", { class: "skel", style: "height:120px" }));
    var parts = [h("div", { class: "page-head rv" }, h("p", { class: "eyebrow", text: "Messages from the PRIME BOT team" }), h("h1", { text: "Drop box" }),
      h("div", null, h("a", { class: "btn sm ghost", href: "#/", text: "Back to servers" }), " ",
        h("button", { class: "btn sm ghost", text: "Mark all read", onclick: function () {
          api("dropbox_read", {}, {}).then(function () { setUnread(0); load(); }).catch(function (e) { toast(e.message, "bad"); });
        } })))];
    if (S.isOwner) parts.push(renderComposer(function () { load(); }));
    parts.push(list);
    app.textContent = "";
    app.appendChild(h("main", { id: "main", class: "page" }, parts));
    function load() {
      api("dropbox").then(function (j) {
        S.inbox = j.messages; setUnread(j.unread); list.textContent = "";
        if (!j.messages.length) return list.appendChild(h("div", { class: "card empty" }, h("h3", { text: "Nothing here yet" }), h("p", { class: "muted", text: "Announcements from the team will show up here." })));
        j.messages.forEach(function (m, i) {
          var card = h("div", { class: "card msg " + m.kind + (m.read ? "" : " unread") + " rv", style: "--i:" + Math.min(i, 10) },
            h("div", { class: "msg-top" }, h("span", { class: "tag", text: KIND_LABEL[m.kind] || "Info" }), !m.read ? h("i", { class: "dot on", title: "Unread" }) : null, h("small", { text: when(m.created_at) })),
            h("h3", { text: m.title }), h("p", { class: "msgbody", text: m.body }));
          var actions = h("div", { class: "msg-actions" });
          if (!m.read) actions.appendChild(h("button", { class: "btn sm ghost", text: "Mark read", onclick: function () {
            api("dropbox_read", {}, { id: m.id }).then(load).catch(function (e) { toast(e.message, "bad"); });
          } }));
          if (S.isOwner) actions.appendChild(h("button", { class: "btn sm ghost danger", text: "Delete for everyone", onclick: function () {
            if (!confirm("Delete this message for every admin?")) return;
            api("dropbox_delete", {}, { id: m.id }).then(load).catch(function (e) { toast(e.message, "bad"); });
          } }));
          card.appendChild(actions); list.appendChild(card);
        });
      }).catch(function (e) { if (e.message !== "401") { list.textContent = ""; list.appendChild(h("p", { text: e.message })); } });
    }
    load();
  }

  /* ---------- welcome card designer (live preview) ---------- */
  function makeWelcomePreview(gid) {
    var img = h("img", { alt: "Welcome card preview", class: "pv-img" });
    var note = h("p", { class: "muted pv-note", text: "Loading preview…" });
    var box = h("div", { class: "card preview rv" },
      h("div", { class: "pv-head" }, h("h2", { text: "Live preview" }), h("small", { text: "Rendered with your name and avatar" })),
      h("div", { class: "pv-stage" }, img), note);
    var timer = null, seq = 0, lastKey = "", hex = /^#[0-9a-f]{6}$/i;
    function update() {
      var d0 = S.draft, key = JSON.stringify([d0.card_theme, d0.avatar_shape, d0.use_template, d0.background_color, d0.accent_color]);
      if (key === lastKey) return;
      lastKey = key; clearTimeout(timer);
      timer = setTimeout(function () {
        var d = S.draft, my = ++seq;
        box.classList.add("busy");
        api("welcome_preview", { guild_id: gid, theme: d.card_theme || "wolf", shape: d.avatar_shape || "circle", use_template: d.use_template === false ? "0" : "1",
          bg: hex.test(d.background_color || "") ? d.background_color : "#2b2d31", accent: hex.test(d.accent_color || "") ? d.accent_color : "#5865F2" })
          .then(function (r) { if (my !== seq) return; img.src = r.image; img.classList.add("ready"); note.textContent = ""; })
          .catch(function (e) {
            if (my !== seq || e.message === "401") return;
            if (e.status === 429) { lastKey = ""; setTimeout(update, 1600); return; }
            note.textContent = e.message;
          }).then(function () { if (my === seq) box.classList.remove("busy"); });
      }, 500);
    }
    return { el: box, update: update };
  }

  /* ---------- audit log ---------- */
  function fmtVal(f, v) {
    if (v == null || v === "") return "not set";
    if (f && f.type === "toggle") return v ? "On" : "Off";
    if (f && (f.type === "channel" || f.type === "role")) {
      var pool = f.type === "role" ? S.meta.roles : [].concat(S.meta.channels.text, S.meta.channels.voice, S.meta.channels.category);
      var hit = pool.filter(function (c) { return c.id === String(v); })[0];
      return hit ? (f.type === "role" ? "@" : "#") + hit.name : "(deleted " + f.type + ")";
    }
    if (f && f.type === "select") { var o = (f.options || []).filter(function (x) { return String(x[0]) === String(v); })[0]; return o ? o[1] : String(v); }
    if (Array.isArray(v)) return v.length ? v.slice(0, 5).join(", ") + (v.length > 5 ? " … +" + (v.length - 5) : "") : "empty list";
    return String(v);
  }
  function renderAudit(gid, main) {
    var list = h("div", { class: "audit" }), moreBtn = h("button", { class: "btn sm ghost", text: "Load more", hidden: true });
    var filter = h("select", { "aria-label": "Filter by area" }, h("option", { value: "", text: "All areas" }), mods().map(function (m) { return h("option", { value: m.id, text: m.title }); }));
    var cursor = null;
    function load(reset) {
      if (reset) { list.textContent = ""; cursor = null; list.appendChild(h("div", { class: "skel", style: "height:90px" })); }
      var params = { guild_id: gid }; if (cursor) params.before = cursor; if (filter.value) params.module = filter.value;
      api("audit", params).then(function (r) {
        if (reset) list.textContent = "";
        if (!r.entries.length && reset) list.appendChild(h("div", { class: "card empty" }, h("h3", { text: "No changes yet" }), h("p", { class: "muted", text: "Changes saved from this dashboard will be listed here." })));
        r.entries.forEach(function (e) {
          var m = modById(e.module), title = m ? m.title : e.module;
          var rows = Object.keys(e.changes || {}).map(function (k) {
            var f = m ? m.fields.filter(function (x) { return x.key === k; })[0] : null, c = e.changes[k];
            return h("li", null, h("b", { text: (f ? f.label : k) + ": " }), h("span", { class: "old", text: fmtVal(f, c.from) }), " → ", h("span", { class: "new", text: fmtVal(f, c.to) }));
          });
          list.appendChild(h("div", { class: "card entry rv" }, h("div", { class: "entry-top" }, h("b", { text: e.user_name }), h("span", { class: "tag", text: title }), h("small", { text: when(e.created_at) })), h("ul", null, rows)));
        });
        if (r.entries.length) cursor = r.entries[r.entries.length - 1].id;
        moreBtn.hidden = !r.more;
      }).catch(function (e) { if (e.message !== "401") { list.textContent = ""; list.appendChild(h("p", { text: e.message })); } });
    }
    filter.addEventListener("change", function () { load(true); });
    moreBtn.addEventListener("click", function () { load(false); });
    add(main, [h("div", { class: "panel-head rv" }, h("div", null, h("p", { class: "crumb", text: "Server" }), h("h1", { text: "Audit log" }),
        h("p", { class: "muted", text: "Who changed which setting, and when. Kept for 180 days. Changes made with slash commands in Discord are not listed." })), filter),
      list, h("div", { class: "more" }, moreBtn)]);
    load(true);
  }

  /* ---------- router ---------- */
  function route() {
    document.body.classList.remove("menu"); S.mod = null;
    var hash = location.hash || "#/", m = hash.match(/^#\/g\/(\d+)(?:\/([a-z]+))?$/);
    if (!S.sid) return renderLogin();
    showAnnouncements();
    if (hash === "#/inbox") { S.guild = null; return renderInbox(); }
    if (!m) { S.guild = null; return renderServers(); }
    var gid = m[1], modId = m[2];
    app.textContent = ""; app.appendChild(h("main", { id: "main", class: "center" }, h("div", { class: "boot", role: "status" }, "Loading server", h("span", { class: "dots" }))));
    renderHeader();
    loadGuild(gid).then(function () {
      var mod = modId ? modById(modId) : null;
      if (modId && modId !== "audit" && !mod) { location.hash = "#/g/" + gid; return; }
      var main = renderShell(gid, modId);
      if (modId === "audit") renderAudit(gid, main); else if (mod) renderModule(gid, mod, main); else renderOverview(gid, main);
      window.scrollTo(0, 0);
    }).catch(function (e) {
      if (e.message === "401") return;
      app.textContent = "";
      app.appendChild(h("main", { id: "main", class: "center" }, h("div", { class: "card empty" }, h("h3", { text: "Can't open this server" }), h("p", { class: "muted", text: e.message }),
        h("a", { class: "btn sm", href: "#/", text: "Back to servers" }))));
    });
  }

  /* ---------- boot ---------- */
  function boot() {
    var frag = new URLSearchParams(location.hash.replace(/^#/, "")), err = null;
    if (frag.get("session")) { localStorage.setItem(KEY, frag.get("session")); history.replaceState(null, "", location.pathname + "#/"); }
    else if (frag.get("error")) { err = frag.get("error"); history.replaceState(null, "", location.pathname); }
    S.sid = localStorage.getItem(KEY);
    fetch(API + "?action=schema", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (j) {
      S.schema = j;
      if (!S.sid) return renderLogin(err);
      return api("me").then(function (me) { S.user = me.user; S.servers = me.servers; S.unread = me.unread || 0; S.isOwner = !!me.is_owner; renderHeader(); route(); });
    }).catch(function (e) { if (e.message !== "401") renderLogin(err || "Can't reach the server right now. Try again in a moment."); });
  }
  window.addEventListener("hashchange", function () { if (S.sid && S.schema && S.user) route(); });
  window.addEventListener("beforeunload", function (e) { if (S.mod !== null && dirtyKeys().length) { e.preventDefault(); e.returnValue = ""; } });
  boot();
})();
