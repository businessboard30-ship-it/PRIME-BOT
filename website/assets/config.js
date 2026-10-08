/* One place to edit site-wide constants. */
window.SITE_CONFIG = {
  // [BOT_INVITE_URL] — OAuth2 invite link. Prefilled from the application ID in config.py (TOPGG_BOT_ID)
  // with the bot's default permission set (discord_clone_service.DEFAULT_INVITE_PERMISSIONS). Verify before launch.
  BOT_INVITE_URL: "https://discord.com/api/oauth2/authorize?client_id=1539561247299604610&permissions=1100317453398&scope=bot%20applications.commands",
  SUPPORT_SERVER_URL: "https://discord.gg/DYfajXrP9B",
  CONTACT_EMAIL: "maxwelldumenya5@outlook.com",
  // [TOPGG_URL] — optional. Leave empty to hide every Top.gg link.
  TOPGG_URL: "",
  // [STATS_API_URL] — optional JSON endpoint: {"servers":123,"commands":87}. Leave empty and the stats strip stays hidden.
  STATS_API_URL: ""
};
