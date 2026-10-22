/* One place for dashboard constants. */
window.DASH_CONFIG = {
  // Backend (Railway `web` service). The dashboard talks to <API_BASE>/api/dash.
  API_BASE: "https://api-service-production-df63.up.railway.app",
  // Public marketing site (links to pricing and docs).
  SITE_URL: "https://prime-bot-site.pages.dev",
  // Invite for PRIME BOT; the dashboard appends &guild_id=<id> so the right server is preselected.
  INVITE_URL: "https://discord.com/api/oauth2/authorize?client_id=1534579332528472246&permissions=1100317453398&scope=bot%20applications.commands",
  SUPPORT_URL: "https://discord.gg/DYfajXrP9B"
};

// Start the TLS handshake with the API now, while the rest of the scripts are still loading,
// so the first request (`me`) doesn't pay for it. connect-src already allows this origin.
(function () {
  try {
    var l = document.createElement("link");
    l.rel = "preconnect"; l.href = window.DASH_CONFIG.API_BASE; l.crossOrigin = "anonymous";
    document.head.appendChild(l);
  } catch (e) { /* purely an optimisation */ }
})();
