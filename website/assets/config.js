/* One place to edit site-wide constants. */
window.SITE_CONFIG = {
  // Invite links (OAuth2, default permission set from discord_clone_service.DEFAULT_INVITE_PERMISSIONS).
  WELCOME_INVITE_URL: "https://discord.com/api/oauth2/authorize?client_id=1539561247299604610&permissions=1100317453398&scope=bot%20applications.commands",
  PRIME_INVITE_URL: "https://discord.com/api/oauth2/authorize?client_id=1534579332528472246&permissions=1100317453398&scope=bot%20applications.commands",
  SUPPORT_SERVER_URL: "https://discord.gg/DYfajXrP9B",
  // Web dashboard (Cloudflare Pages project prime-bot-dash). Links marked data-dash use this.
  DASHBOARD_URL: "https://prime-bot-dash.pages.dev",
  CONTACT_EMAIL: "maxwelldumenya5@outlook.com",
  // Top.gg listing + vote page. Leave empty to hide every Top.gg link.
  TOPGG_URL: "https://top.gg/bot/1539561247299604610",
  TOPGG_VOTE_URL: "https://top.gg/bot/1539561247299604610/vote",
  // DiscordThings (dsc.sh) listing page. Paste your bot's dsc.sh page URL here; empty hides its links.
  DTHINGS_URL: "",
  // Public JSON endpoint (api/public_stats.py): {"servers":123}. Feeds the stats strip and the status page.
  STATS_API_URL: "https://web-production-74667a.up.railway.app/api/public_stats"
};
